"""训练服务：按需重训并把候选落到模型版本目录。

与推理服务共用同一套依赖与 `prediction` 包，只是入口不同：
- 这是长驻容器，收到 `POST /train` 才跑一次训练；
- 训练产出写到 `/work/<run_id>/`，过准入后才复制到 `/model/versions/<版本>/`；
- 它不改 `current` 软链、不调 reload——切换与回退由 S03 负责。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

MODEL_DIR = Path(os.environ.get("STAGE3_MODEL_DIR", "/model"))
WORK_DIR = Path(os.environ.get("TRAINER_WORK_DIR", "/work"))
TRAIN_TIMEOUT_SECONDS = int(os.environ.get("TRAIN_TIMEOUT_SECONDS", "1800"))

app = FastAPI(title="PowerSystem Trainer", version="1.0")


class TrainRequest(BaseModel):
    days: int = Field(default=120, ge=10, le=365)
    seed: int = 2026


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "model_dir": str(MODEL_DIR),
        "work_dir": str(WORK_DIR),
        "versions_writable": os.access(MODEL_DIR / "versions", os.W_OK) if (MODEL_DIR / "versions").exists() else False,
    }


@app.post("/train")
def train(request: TrainRequest) -> dict[str, object]:
    """跑一次窗口模式训练，产出候选并（过准入时）复制到版本目录。"""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    out = WORK_DIR / run_id
    out.mkdir(parents=True, exist_ok=True)
    started = time.time()
    command = [
        sys.executable, "-m", "prediction.retrain",
        "--out-dir", str(out),
        "--synthetic", "--window-mode",
        "--days", str(request.days), "--seed", str(request.seed),
    ]
    try:
        proc = subprocess.run(command, cwd="/app", capture_output=True, text=True, timeout=TRAIN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise HTTPException(
            504, {"ok": False, "reason": "timeout", "run_id": run_id, "seconds": TRAIN_TIMEOUT_SECONDS}
        ) from error

    report_path = out / "retrain-report.json"
    if not report_path.is_file():
        raise HTTPException(500, {
            "ok": False, "reason": "no_report", "run_id": run_id,
            "log": ((proc.stdout or "") + (proc.stderr or ""))[-2000:],
        })

    report = json.loads(report_path.read_text(encoding="utf-8"))
    artifact = ""
    if report.get("eligible"):
        version = str(report["model_version"])
        destination = MODEL_DIR / "versions" / version
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("model.json", "metadata.json"):
            shutil.copy2(out / version / name, destination / name)
        artifact = str(destination)

    return {
        "ok": True,
        "run_id": run_id,
        "seconds": round(time.time() - started, 1),
        "eligible": bool(report.get("eligible")),
        "rejection_reason": report.get("rejection_reason"),
        "report": report,
        "artifact_path": artifact,
        "log": (proc.stdout or "")[-1200:],
    }
