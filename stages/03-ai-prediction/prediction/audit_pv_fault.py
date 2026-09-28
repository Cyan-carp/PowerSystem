"""Audit the original Costa photovoltaic MAT files without inventing timestamps."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.io import loadmat

FIELDS = {"dataset_amb.mat": ("f_nv", "irr", "pvt"),
          "dataset_elec.mat": ("idc1", "idc2", "vdc1", "vdc2")}


def audit(directory: Path) -> dict[str, object]:
    arrays = {}
    files = []
    for filename, names in FIELDS.items():
        path = directory / filename
        content = loadmat(path, variable_names=names)
        files.append({"name": filename, "bytes": path.stat().st_size,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        for name in names:
            arrays[name] = np.asarray(content[name]).reshape(-1)
    lengths = {name: len(values) for name, values in arrays.items()}
    if len(set(lengths.values())) != 1:
        raise ValueError("MAT arrays have unequal lengths")
    labels = arrays["f_nv"]
    if not np.all(np.isfinite(labels)) or not np.all(labels == np.floor(labels)):
        raise ValueError("fault labels contain missing or non-integer values")
    classes, counts = np.unique(labels.astype(np.int8), return_counts=True)
    if not set(classes).issubset({0, 1, 2, 3, 4}):
        raise ValueError("unexpected fault class")
    boundaries = np.r_[0, np.flatnonzero(labels[1:] != labels[:-1]) + 1, len(labels)]
    runs = [{"class": int(labels[start]), "start_index": int(start),
             "length_samples": int(end - start)}
            for start, end in zip(boundaries[:-1], boundaries[1:])]
    onsets = [run for run, previous in zip(runs[1:], runs[:-1])
              if previous["class"] == 0 and run["class"] != 0]
    fields = {}
    for name, values in arrays.items():
        finite = np.isfinite(values)
        fields[name] = {"samples": len(values), "missing_or_nonfinite": int((~finite).sum()),
                        "minimum": float(np.min(values[finite])) if finite.any() else None,
                        "maximum": float(np.max(values[finite])) if finite.any() else None}
    return {
        "source": "https://github.com/clayton-h-costa/pv_fault_dataset",
        "license": "Apache-2.0 (repository LICENSE)",
        "files": files, "sample_rows": len(labels), "fields": fields,
        "label_counts": {str(int(k)): int(v) for k, v in zip(classes, counts)},
        "label_transition_runs": len(runs),
        "normal_to_fault_transitions": len(onsets),
        "normal_to_fault_by_class": {str(code): sum(run["class"] == code for run in onsets)
                                      for code in range(1, 5)},
        "first_onsets_index_only": onsets[:20],
        "time_axis": "No timestamps or day boundaries are present in either MAT file. The paper states 1 Hz during data collection; sample indices alone cannot establish continuous elapsed time across 16 days.",
        "one_hour_risk_validated": False,
        "reason": "No verified wall-clock onset or continuity; experimentally introduced faults and module rather than inverter temperature also differ from the trained target.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.directory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: result[key] for key in
                      ("sample_rows", "label_counts", "normal_to_fault_transitions",
                       "normal_to_fault_by_class", "one_hour_risk_validated")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
