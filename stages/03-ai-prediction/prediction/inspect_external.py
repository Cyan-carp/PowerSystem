"""Inventory a downloaded dataset before claiming one-hour fault labels."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def inspect(path: Path) -> dict[str, object]:
    files = [path] if path.is_file() else sorted(p for p in path.rglob("*") if p.is_file())
    result: dict[str, object] = {"source_path": str(path), "files": [], "one_hour_risk_validated": False}
    for file in files:
        suffix = file.suffix.lower()
        item: dict[str, object] = {"name": file.name, "bytes": file.stat().st_size}
        if suffix == ".csv":
            with file.open("r", encoding="utf-8-sig", newline="", errors="replace") as stream:
                reader = csv.reader(stream)
                item["columns"] = next(reader, [])
                item["preview_rows"] = [row[:8] for _, row in zip(range(2), reader)]
        elif suffix == ".mat":
            try:
                from scipy.io import whosmat
                item["arrays"] = [{"name": name, "shape": shape, "type": dtype}
                                  for name, shape, dtype in whosmat(file)]
            except Exception as error:
                item["inspection_error"] = str(error)
        else:
            continue
        result["files"].append(item)
    result["next_check"] = (
        "Map timestamps and fault-onset labels, count independent events, then verify "
        "that a 30-minute causal window can precede onset by up to 60 minutes. "
        "Current fault-class labels alone do not establish one-hour prediction validity."
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if not args.path.exists():
        parser.error(f"dataset not found: {args.path}")
    report = inspect(args.path)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
