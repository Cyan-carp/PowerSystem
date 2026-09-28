"""Summarize global feature contributions on the saved chronological test split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import xgboost as xgb

from .features import FEATURE_NAMES
from .train import load_windows


def report(data_dir: Path, model_dir: Path) -> dict[str, object]:
    evaluation = json.loads((model_dir / "evaluation.json").read_text(encoding="utf-8"))
    metadata = json.loads((model_dir / "metadata.json").read_text(encoding="utf-8"))
    if list(FEATURE_NAMES) != metadata["feature_names"]:
        raise ValueError("saved model features differ from the current extractor")
    days, features, labels = load_windows(data_dir)
    test_days = set(evaluation["split"]["test"]["days"])
    selection = np.asarray([day in test_days for day in days])
    if int(selection.sum()) != evaluation["split"]["test"]["samples"]:
        raise ValueError("saved test split size differs from reconstructed windows")
    if int(labels[selection].sum()) != evaluation["split"]["test"]["positives"]:
        raise ValueError("saved test labels differ from reconstructed windows")
    booster = xgb.Booster()
    booster.load_model(model_dir / "model.json")
    if booster.feature_names != list(FEATURE_NAMES):
        raise ValueError("model feature order differs from saved metadata")
    matrix = xgb.DMatrix(features[selection], feature_names=list(FEATURE_NAMES))
    contributions = booster.predict(matrix, pred_contribs=True)
    magnitudes = np.abs(contributions[:, :-1]).mean(axis=0)
    ranking = sorted(({"feature": name, "mean_abs_log_odds_contribution": float(value)}
                      for name, value in zip(FEATURE_NAMES, magnitudes)),
                     key=lambda item: item["mean_abs_log_odds_contribution"], reverse=True)
    return {
        "model_version": evaluation["model_version"],
        "method": "mean absolute XGBoost TreeSHAP contribution in raw log-odds; bias excluded",
        "test_days": sorted(test_days),
        "test_windows": int(selection.sum()),
        "ranking": ranking,
        "interpretation_limit": "Global magnitude is not a causal fault explanation or field validation.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = report(args.data, args.model)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"model_version": result["model_version"], "test_windows": result["test_windows"],
                      "ranking": result["ranking"][:5]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
