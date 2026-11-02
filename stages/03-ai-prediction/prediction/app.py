"""Loopback FastAPI inference service for the trained stage-three model.

模型目录支持两种布局：

- **新布局**：``<MODEL_DIR>/current`` 是指向 ``versions/<v>/`` 的软链，切换版本只改软链；
- **旧布局**：``<MODEL_DIR>/{model.json,metadata.json}`` 直接放文件，用于兼容改造前的部署。

``POST /admin/reload`` 重新加载模型：先完整构造新的 ``Runtime``，成功后才替换全局引用，
失败时旧模型继续服务——请求线程要么看到旧模型、要么看到新模型，不会看到半个。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import numpy as np
import xgboost as xgb
from scipy.special import expit
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .features import FEATURE_NAMES, extract

if "STAGE3_MODEL_DIR" in os.environ:
    MODEL_DIR = Path(os.environ["STAGE3_MODEL_DIR"])
else:
    MODEL_DIR = Path(__file__).resolve().parents[3] / "artifacts" / "stage3" / "model"
app = FastAPI(title="PowerSystem Fault Risk", version="1.1")


class Sample(BaseModel):
    ts_ms: int
    voltage: float
    current: float
    temperature: float
    power: float


class PredictionRequest(BaseModel):
    device_id: int = Field(gt=0)
    window_end_ms: int = Field(gt=0)
    samples: list[Sample] = Field(min_length=27, max_length=500)


class ReloadRequest(BaseModel):
    """缺省 version 时读 current 软链指向的目录。"""

    version: str | None = None


class Runtime:
    def __init__(self, directory: Path) -> None:
        self.source_path = str(directory)
        self.loaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
        if tuple(self.metadata["feature_names"]) != FEATURE_NAMES:
            raise ValueError("model feature schema differs from inference code")
        self.model = xgb.Booster()
        self.model.load_model(directory / "model.json")

    def predict(self, request: PredictionRequest) -> dict[str, object]:
        vector = extract([sample.model_dump() for sample in request.samples], request.window_end_ms)
        ordered = np.asarray([[vector[name] for name in FEATURE_NAMES]], dtype=np.float32)
        matrix = xgb.DMatrix(ordered, feature_names=list(FEATURE_NAMES))
        margin = float(self.model.predict(matrix, output_margin=True)[0])
        calibration = self.metadata["calibration"]
        probability = float(expit(calibration["slope"] * margin + calibration["intercept"]))
        contributions = self.model.predict(matrix, pred_contribs=True)[0][:-1]
        ranked = sorted(zip(FEATURE_NAMES, contributions), key=lambda item: abs(float(item[1])), reverse=True)[:5]
        threshold = float(self.metadata["threshold"])
        return {
            "device_id": request.device_id,
            "window_end_ms": request.window_end_ms,
            "probability": probability,
            "threshold": threshold,
            "risk_level": "high" if probability >= threshold else "low",
            "model_version": self.metadata["model_version"],
            "top_factors": [{"feature": name, "value": vector[name], "contribution": float(weight)} for name, weight in ranked],
            "source": "synthetic-trained",
        }


def resolve_model_dir(version: str | None = None) -> Path:
    """定位要加载的目录；找不到或断链时抛 FileNotFoundError，由调用方转成 404。"""
    if version:
        candidate = MODEL_DIR / "versions" / version
        if not candidate.is_dir():
            raise FileNotFoundError(f"model version not found: {version}")
        return candidate
    current = MODEL_DIR / "current"
    if current.is_symlink() or current.is_dir():
        target = current.resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"current points to a missing directory: {target}")
        return target
    # 旧布局：兼容未做目录迁移的部署
    if (MODEL_DIR / "model.json").is_file():
        return MODEL_DIR
    raise FileNotFoundError(f"no model found under {MODEL_DIR}")


_runtime: Runtime | None = None
_lock = threading.Lock()


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        with _lock:
            if _runtime is None:
                _runtime = Runtime(resolve_model_dir(None))
    return _runtime


def current_version() -> str:
    try:
        return str(runtime().metadata["model_version"])
    except Exception:  # noqa: BLE001 - 健康检查不该因模型问题抛栈
        return ""


@app.get("/health")
def health() -> dict[str, object]:
    try:
        current = runtime()
        return {
            "status": "ok",
            "model_version": str(current.metadata["model_version"]),
            "loaded_at": current.loaded_at,
            "source_path": current.source_path,
        }
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"model unavailable: {error}") from error


def switch_current(version: str) -> None:
    """原子切换 current 软链：先建临时链再 rename，读方不会看到半成品。

    要求 /model 可写；只读挂载时会抛 OSError，由调用方决定是否中止。
    """
    current = MODEL_DIR / "current"
    staging = MODEL_DIR / ".current.tmp"
    if staging.is_symlink() or staging.exists():
        staging.unlink()
    staging.symlink_to(f"versions/{version}")
    os.replace(staging, current)


@app.post("/admin/reload")
def admin_reload(body: ReloadRequest | None = None) -> dict[str, object]:
    """重新加载模型。成功才替换运行时引用，失败保留旧模型继续服务。

    传 version 时会顺带切换 current 软链——顺序必须是「先加载成功、再切软链」，
    这样加载失败时软链与内存中的模型都停在旧版本，不会出现「软链指向坏目录、
    下次重启起不来」的情况。
    """
    global _runtime
    version = body.version if body else None
    with _lock:
        previous = str(_runtime.metadata["model_version"]) if _runtime else ""
        try:
            candidate = Runtime(resolve_model_dir(version))
        except FileNotFoundError as error:
            raise HTTPException(
                status_code=404,
                detail={"ok": False, "reason": "version_not_found", "detail": str(error), "previous": previous},
            ) from error
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=409,
                detail={"ok": False, "reason": "load_failed", "detail": str(error), "previous": previous},
            ) from error
        switched = False
        if version:
            try:
                switch_current(version)
                switched = True
            except OSError as error:
                # 只读挂载或权限不足：模型已加载但软链没动，重启后会回到旧版本，必须明确告知
                raise HTTPException(
                    status_code=409,
                    detail={
                        "ok": False, "reason": "switch_readonly", "detail": str(error),
                        "previous": previous, "loaded_but_not_persisted": True,
                    },
                ) from error
        _runtime = candidate
        return {
            "ok": True,
            "model_version": str(candidate.metadata["model_version"]),
            "previous": previous,
            "loaded_at": candidate.loaded_at,
            "source_path": candidate.source_path,
            "link_switched": switched,
        }


@app.post("/predict")
def predict(request: PredictionRequest) -> dict[str, object]:
    try:
        return runtime().predict(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"model unavailable: {error}") from error
