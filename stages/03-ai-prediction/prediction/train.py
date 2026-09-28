"""Train and evaluate one-hour fault-onset risk using chronological days."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xgboost as xgb
from scipy.optimize import minimize
from scipy.special import expit

from .features import FEATURE_NAMES, extract

HOUR_MS = 60 * 60 * 1000
STEP_MINUTES = 5


def metrics(labels: np.ndarray, probabilities: np.ndarray, threshold: float) -> dict[str, float | int | dict[str, int]]:
    actual = labels.astype(bool)
    predicted = probabilities >= threshold
    tp = int(np.sum(actual & predicted))
    tn = int(np.sum(~actual & ~predicted))
    fp = int(np.sum(~actual & predicted))
    fn = int(np.sum(actual & ~predicted))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "samples": len(labels),
        "positives": int(np.sum(actual)),
        "accuracy": (tp + tn) / len(labels) if len(labels) else 0.0,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "f2": 5 * precision * recall / (4 * precision + recall) if 4 * precision + recall else 0.0,
        "false_alarm_rate": fp / (fp + tn) if fp + tn else 0.0,
        "confusion_matrix": {"tn": tn, "fp": fp, "fn": fn, "tp": tp},
    }


def fit_calibration(margins: np.ndarray, labels: np.ndarray) -> dict[str, float]:
    """Fit a monotone logistic map on validation data only."""
    # XGBoost returns float32 margins. SciPy's finite-difference step must
    # operate on float64 values or the objective appears locally constant.
    margins = margins.astype(np.float64)
    target = labels.astype(np.float64)

    def objective(params: np.ndarray) -> float:
        slope = float(np.exp(params[0]))
        logits = slope * margins + params[1]
        return float(np.mean(np.logaddexp(0, logits) - target * logits) + 1e-3 * np.sum(params ** 2))

    optimum = minimize(objective, np.array([0.0, -2.0]), method="BFGS")
    if not optimum.success:
        raise RuntimeError(f"probability calibration failed: {optimum.message}")
    return {"slope": float(np.exp(optimum.x[0])), "intercept": float(optimum.x[1])}


def calibrate(margins: np.ndarray, calibration: dict[str, float]) -> np.ndarray:
    return expit(calibration["slope"] * margins + calibration["intercept"])


def load_windows(data_dir: Path) -> tuple[list[str], np.ndarray, np.ndarray]:
    events: dict[tuple[str, str], dict[str, object]] = {}
    with (data_dir / "fault-events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            # The key is derived from the local date of the telemetry below.
            events[(event["device_id"], str(event["onset_ms"]))] = event
    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    with gzip.open(data_dir / "telemetry.csv.gz", "rt", encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            # Every generated day has exactly 1440 minute samples; the local day
            # number is stable even when its UTC date differs.
            local_day = datetime.fromtimestamp(int(row["ts_ms"]) / 1000, timezone.utc).astimezone(
                timezone_from_offset()
            ).date().isoformat()
            grouped[(row["device_id"], local_day)].append(row)
    onsets: dict[tuple[str, str], dict[str, object]] = {}
    for event in events.values():
        day = datetime.fromtimestamp(event["onset_ms"] / 1000, timezone_from_offset()).date().isoformat()
        onsets[(event["device_id"], day)] = event
    days: list[str] = []
    features: list[list[float]] = []
    labels: list[int] = []
    for key in sorted(grouped):
        rows = sorted(grouped[key], key=lambda row: int(row["ts_ms"]))
        event = onsets[key]
        onset = int(event["onset_ms"])
        recovery = int(event["recovery_ms"])
        for end_index in range(29, len(rows), STEP_MINUTES):
            end_ms = int(rows[end_index]["ts_ms"])
            if onset <= end_ms < recovery + 30 * 60_000:
                continue
            window = rows[end_index - 29:end_index + 1]
            vector = extract(window, end_ms)
            days.append(key[1])
            features.append([vector[name] for name in FEATURE_NAMES])
            labels.append(int(end_ms < onset <= end_ms + HOUR_MS))
    return days, np.asarray(features, dtype=np.float32), np.asarray(labels, dtype=np.int8)


def timezone_from_offset():
    from datetime import timedelta
    return timezone(timedelta(hours=8))


def train(data_dir: Path, out: Path) -> dict[str, object]:
    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    days, features, labels = load_windows(data_dir)
    unique_days = sorted(set(days))
    if len(unique_days) < 10:
        raise ValueError("need at least 10 distinct days")
    train_end = int(len(unique_days) * 0.6)
    val_end = int(len(unique_days) * 0.8)
    subsets = {
        "train": np.array([d < unique_days[train_end] for d in days]),
        "validation": np.array([unique_days[train_end] <= d < unique_days[val_end] for d in days]),
        "test": np.array([d >= unique_days[val_end] for d in days]),
    }
    if any(len(np.unique(labels[mask])) < 2 for mask in subsets.values()):
        raise ValueError("every chronological split needs positive and negative examples")
    matrices = {name: xgb.DMatrix(features[mask], label=labels[mask], feature_names=list(FEATURE_NAMES)) for name, mask in subsets.items()}
    trials = [
        {"max_depth": 3, "eta": 0.08, "num_boost_round": 80},
        {"max_depth": 4, "eta": 0.06, "num_boost_round": 120},
        {"max_depth": 5, "eta": 0.05, "num_boost_round": 160},
    ]
    evaluated = []
    best: tuple[float, xgb.Booster, float, dict[str, object], dict[str, float]] | None = None
    for params in trials:
        booster = xgb.train(
            {"objective": "binary:logistic", "eval_metric": "logloss", "tree_method": "hist",
             "max_depth": params["max_depth"], "eta": params["eta"], "seed": int(manifest["seed"]),
             "nthread": 4, "scale_pos_weight": max(1.0, float(np.sum(labels[subsets["train"]] == 0)) / float(np.sum(labels[subsets["train"]] == 1)))},
            matrices["train"],
            num_boost_round=params["num_boost_round"],
            verbose_eval=False,
        )
        val_margin = booster.predict(matrices["validation"], output_margin=True)
        calibration = fit_calibration(val_margin, labels[subsets["validation"]])
        val_probs = calibrate(val_margin, calibration)
        candidates = [(float(metrics(labels[subsets["validation"]], val_probs, threshold)["f2"]), threshold,
                       metrics(labels[subsets["validation"]], val_probs, threshold))
                      for threshold in np.arange(0.05, 0.951, 0.025)]
        limited = [item for item in candidates if float(item[2]["false_alarm_rate"]) <= 0.1]
        score, threshold, val_metrics = max(limited or candidates, key=lambda item: item[0])
        evaluated.append({"params": params, "calibration": calibration, "threshold": threshold, "validation": val_metrics})
        if best is None or score > best[0]:
            best = (score, booster, threshold, params, calibration)
    assert best is not None
    _, booster, threshold, parameters, calibration = best
    test_probs = calibrate(booster.predict(matrices["test"], output_margin=True), calibration)
    test_metrics = metrics(labels[subsets["test"]], test_probs, threshold)
    version_bytes = booster.save_raw(raw_format="json") + json.dumps({"calibration": calibration, "threshold": threshold}).encode()
    model_version = "stage3-xgb-" + hashlib.sha256(version_bytes).hexdigest()[:12]
    split = {name: {"days": sorted({d for d, keep in zip(days, mask) if keep}),
                    "samples": int(mask.sum()), "positives": int(labels[mask].sum())}
             for name, mask in subsets.items()}
    report: dict[str, object] = {
        "model_version": model_version,
        "target": "first fault onset strictly after window end and within 60 minutes",
        "feature_window_minutes": 30,
        "feature_step_minutes": STEP_MINUTES,
        "source": manifest,
        "split": split,
        "trials": evaluated,
        "selected_params": parameters,
        "calibration": calibration,
        "threshold": threshold,
        "test": test_metrics,
        "note": "Synthetic holdout metrics measure the synthetic scenario, not field reliability.",
    }
    out.mkdir(parents=True, exist_ok=True)
    booster.save_model(out / "model.json")
    (out / "metadata.json").write_text(json.dumps({"model_version": model_version, "threshold": threshold,
                                                     "calibration": calibration,
                                                     "feature_names": FEATURE_NAMES}, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "evaluation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    matrix = test_metrics["confusion_matrix"]
    (out / "evaluation.md").write_text(
        f"# 阶段三模型评估\n\n数据：确定性合成逆变器；测试日期晚于训练及验证日期。\n\n"
        f"目标：窗口结束后一小时内的首次故障。窗口 30 分钟，步长 5 分钟。\n\n"
        f"测试样本 {test_metrics['samples']}，正样本 {test_metrics['positives']}；阈值 {threshold:.3f}。\n\n"
        f"| 指标 | 结果 |\n| --- | ---: |\n| 准确率 | {test_metrics['accuracy']:.3f} |\n"
        f"| 精确率 | {test_metrics['precision']:.3f} |\n| 召回率 | {test_metrics['recall']:.3f} |\n"
        f"| F1 | {test_metrics['f1']:.3f} |\n| 误报率 | {test_metrics['false_alarm_rate']:.3f} |\n\n"
        f"混淆矩阵：TN={matrix['tn']}、FP={matrix['fp']}、FN={matrix['fn']}、TP={matrix['tp']}。\n\n"
        "以上仅证明合成数据表现，不能宣称真实场站准确率。\n",
        encoding="utf-8",
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = train(args.data, args.out)
    print(json.dumps({"model_version": report["model_version"], "threshold": report["threshold"], "test": report["test"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
