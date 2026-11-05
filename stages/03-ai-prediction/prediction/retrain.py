"""重训作业：产出一个候选模型目录和一份机器可读报告。

本脚本只产候选，不切换生产模型：
- 只写 ``--out-dir`` 与训练期间的临时目录，从不写入 ``STAGE3_MODEL_DIR`` 指向的生产目录；
- 推理服务 ``prediction.app`` 启动时只加载一次模型、没有热更新，所以换模型的唯一方式是
  把 ``<out-dir>/<model_version>/`` 里的 ``model.json`` 与 ``metadata.json`` 覆盖到生产模型目录，
  再重启容器。该切换动作由运维/后端完成，本脚本不做；
- 准入判断（样本量、正样本量、F1）写在脚本里，结果由 ``eligible`` 与 ``rejection_reason`` 表达，
  外层调度按退出码决定是否登记 ``model_registry``。

复用 ``prediction.train.train`` 与 ``prediction.generate.generate``，不重写训练与合成逻辑。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import date, datetime, timezone
from pathlib import Path

from . import generate as generate_module
from . import train as train_module

# 准入阈值：与调度侧约定一致，改动需同步后端登记逻辑。
MIN_TRAIN_SAMPLES = 5000
MIN_POSITIVE_SAMPLES = 50
MIN_F1 = 0.5

# 退出码：0 可登记；2 训练成功但未过准入；1 异常（参数、生成、训练、写盘失败）。
EXIT_ELIGIBLE = 0
EXIT_ERROR = 1
EXIT_REJECTED = 2

# 合成数据固定起始日期，保证同 seed 下可复现（与 generate.py 的默认值保持一致）。
SYNTHETIC_START = date(2026, 1, 1)

# 从训练报告复制到候选目录的文件，正是 app.py 启动时读取的两个文件。
CANDIDATE_FILES = ("model.json", "metadata.json")


def log(payload: dict[str, object]) -> None:
    """按 train.py 的风格输出单行 JSON 日志。"""
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def resolve_counts(report: dict[str, object]) -> tuple[int, int, str, str]:
    """从训练报告里取样本总量与正样本量。

    train.py 当前只给出 ``split.<名称>.samples/positives``，没有顶层汇总字段。
    因此先尝试顶层 ``train_samples`` / ``positive_samples``，缺失时按契约用
    test + validation + train 三个切分求和兜底，并把取值方式写回报告。
    两个数字用同一口径（同一批切分），正样本占比才有意义。
    """
    split = report.get("split") or {}
    totals = {
        field: sum(int(entry.get(field) or 0) for entry in split.values())
        for field in ("samples", "positives")
    }

    samples = report.get("train_samples")
    sample_source = "report.train_samples"
    if samples is None:
        samples = totals["samples"]
        sample_source = "split_sum"

    positives = report.get("positive_samples")
    positive_source = "report.positive_samples"
    if positives is None:
        positives = totals["positives"]
        positive_source = "split_sum"

    return int(samples), int(positives), sample_source, positive_source


def judge(samples: int, positives: int, f1: float) -> tuple[bool, str | None]:
    """准入判断：按顺序命中第一条不满足的条件。"""
    if samples < MIN_TRAIN_SAMPLES:
        return False, "insufficient_samples"
    if positives < MIN_POSITIVE_SAMPLES:
        return False, "insufficient_positives"
    if f1 < MIN_F1:
        return False, "low_f1"
    return True, None


def guard_out_dir(out_dir: Path) -> None:
    """拒绝把候选写到生产模型目录（或其子目录），避免误覆盖现役模型。"""
    production = os.environ.get("STAGE3_MODEL_DIR")
    if not production:
        return
    target = out_dir.resolve()
    current = Path(production).resolve()
    if target == current or current in target.parents:
        raise ValueError(f"--out-dir 不能落在生产模型目录 {current} 内，本脚本只产候选模型")


def build_model(args: argparse.Namespace) -> tuple[Path, Path | None]:
    """准备训练数据目录：合成时生成到临时目录，否则用 --data-dir。

    返回 (数据目录, 需要清理的临时目录或 None)。
    """
    if args.synthetic:
        if args.data_dir is not None:
            raise ValueError("--synthetic 与 --data-dir 不能同时使用")
        temp_root = Path(tempfile.mkdtemp(prefix="stage3-retrain-"))
        manifest = generate_module.generate(
            temp_root / "synthetic", args.days, SYNTHETIC_START, args.seed,
            window_mode=args.window_mode,
            normal_voltage_diverse=args.normal_voltage_diverse,
        )
        log({"step": "generate", "days": args.days, "seed": args.seed, "rows": manifest["rows"],
             "fault_events": manifest["fault_events"]})
        return temp_root / "synthetic", temp_root
    if args.data_dir is None:
        raise ValueError("必须提供 --data-dir 或使用 --synthetic")
    data_dir = Path(args.data_dir)
    if not (data_dir / "manifest.json").is_file():
        raise ValueError(f"数据目录缺少 manifest.json：{data_dir}")
    return data_dir, None


def publish(report: dict[str, object], staging: Path, out_dir: Path) -> Path:
    """把训练产物复制成 <out-dir>/<model_version>/ 候选目录。"""
    version = str(report["model_version"])
    candidate = out_dir / version
    candidate.mkdir(parents=True, exist_ok=True)
    for name in CANDIDATE_FILES:
        shutil.copy2(staging / name, candidate / name)
    return candidate


def retrain(args: argparse.Namespace) -> dict[str, object]:
    """执行一次重训，返回写盘用的报告字典（不负责写文件）。"""
    out_dir = Path(args.out_dir)
    guard_out_dir(out_dir)

    data_dir: Path | None = None
    temp_root: Path | None = None
    staging: Path | None = None
    try:
        data_dir, temp_root = build_model(args)
        # 合成数据的生成参数随临时目录一起被清理，这里先把关键生成参数写进报告留存。
        generation = None
        if args.synthetic:
            gen_manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
            generation = {
                "synthetic": True,
                "days": gen_manifest.get("days"),
                "start_date": gen_manifest.get("start_date"),
                "seed": gen_manifest.get("seed"),
                "fault_mode": gen_manifest.get("fault_mode"),
                "normal_voltage_diverse": gen_manifest.get("normal_voltage_diverse"),
                "voltage_sigma_range": gen_manifest.get("voltage_sigma_range"),
                "sampling_seconds": gen_manifest.get("sampling_seconds"),
                "rows": gen_manifest.get("rows"),
                "fault_events": gen_manifest.get("fault_events"),
            }
        # 训练先落到临时目录：model_version 要训练后才知道，不能直接写最终路径。
        staging = Path(tempfile.mkdtemp(prefix="stage3-model-"))
        log({"step": "train", "data_dir": str(data_dir), "synthetic": bool(args.synthetic)})
        train_report = train_module.train(data_dir, staging)

        candidate = publish(train_report, staging, out_dir)
        log({"step": "publish", "candidate": str(candidate), "model_version": train_report["model_version"]})

        samples, positives, sample_source, positive_source = resolve_counts(train_report)
        test = train_report["test"]
        metrics = {
            "accuracy": test["accuracy"],
            "precision": test["precision"],
            "recall": test["recall"],
            "f1": test["f1"],
            "samples": test["samples"],
            "positives": test["positives"],
        }
        eligible, reason = judge(samples, positives, float(metrics["f1"]))
        return {
            "model_version": train_report["model_version"],
            "baseline_version": args.baseline_version,
            "data_source": "synthetic" if args.synthetic else "provided",
            "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "train_samples": samples,
            "positive_samples": positives,
            "threshold": train_report["threshold"],
            "metrics": metrics,
            # 现役版本的指标需要现役模型在同一测试集上复算，本轮不读取生产模型，因此留空。
            "baseline_metrics": None,
            "generation": generation,
            "eligible": eligible,
            "rejection_reason": reason,
            # 以下两个字段说明上两个数字的取值方式，供审计核对。
            "sample_source": sample_source,
            "positive_source": positive_source,
        }
    finally:
        # 只清理自己创建的临时目录，绝不触碰 --out-dir 之外的用户目录。
        for path in (temp_root, staging):
            if path is not None:
                shutil.rmtree(path, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", type=Path, required=True, help="候选模型输出目录")
    parser.add_argument("--data-dir", type=Path, default=None, help="训练数据目录；不给则用 --synthetic 生成")
    parser.add_argument("--synthetic", action="store_true", help="先生成合成数据再训练")
    parser.add_argument("--days", type=int, default=180, help="合成数据天数，默认 180（generate.py 要求至少 10）")
    parser.add_argument("--seed", type=int, default=2026, help="合成数据种子，默认 2026")
    parser.add_argument(
        "--window-mode", action="store_true",
        help="按生产窗口注入生成突变型故障（注入前 60 分钟合成前兆，前兆期标签仍正常），与 simulator.py 的 WINDOW_SCENARIOS 对齐",
    )
    parser.add_argument(
        "--normal-voltage-diverse", action="store_true",
        help="正常样本按设备/天取确定性 voltage_std∈[0.8,1.5]（含1.1-1.3），修复正常波动被误判为故障的训练偏差；故障注入语义不变",
    )
    parser.add_argument("--baseline-version", default=None, help="现役版本号，仅写进报告，不参与训练")
    parser.add_argument("--report", type=Path, default=None, help="报告输出路径，默认 <out-dir>/retrain-report.json")
    args = parser.parse_args(argv)

    report_path = Path(args.report) if args.report is not None else Path(args.out_dir) / "retrain-report.json"
    try:
        report = retrain(args)
    except Exception as error:  # 参数、生成、训练、写盘失败统一按异常返回 1
        log({"error": f"{type(error).__name__}: {error}", "traceback": traceback.format_exc()})
        return EXIT_ERROR

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log({"step": "report", "path": str(report_path), "eligible": report["eligible"],
         "rejection_reason": report["rejection_reason"]})
    log({"model_version": report["model_version"], "threshold": report["threshold"],
         "train_samples": report["train_samples"], "positive_samples": report["positive_samples"],
         "metrics": report["metrics"], "eligible": report["eligible"]})
    return EXIT_ELIGIBLE if report["eligible"] else EXIT_REJECTED


if __name__ == "__main__":
    sys.exit(main())
