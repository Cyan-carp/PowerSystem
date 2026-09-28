"""Loopback FastAPI inference service for the trained stage-three model."""

from __future__ import annotations

import json
import os
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
app = FastAPI(title="PowerSystem Fault Risk", version="1.0")


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


class Runtime:
    def __init__(self, directory: Path) -> None:
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


_runtime: Runtime | None = None


def runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime(MODEL_DIR)
    return _runtime


@app.get("/health")
def health() -> dict[str, str]:
    try:
        return {"status": "ok", "model_version": str(runtime().metadata["model_version"])}
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"model unavailable: {error}") from error


@app.post("/predict")
def predict(request: PredictionRequest) -> dict[str, object]:
    try:
        return runtime().predict(request)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=503, detail=f"model unavailable: {error}") from error
