"""Offline gap triage. Original JSONL records are never edited."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.misses import Misses


def summarize(directory):
    groups = {}
    errors = 0
    for path in sorted(directory.glob("misses-????????.jsonl")):
        if path.is_symlink():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                question = row["normalized_question"]
                key = hashlib.sha256(question.encode()).hexdigest()[:20]
                item = groups.setdefault(key, {"id": key, "question": question, "count": 0, "reasons": [], "last_seen": "",
                    "sources": [], "source_details": [], "web_statuses": [], "request_ids": [], "knowledge_followup": "pending"})
                item["count"] += 1
                item["last_seen"] = max(item["last_seen"], row["at"])
                if row["reason"] not in item["reasons"]:
                    item["reasons"].append(row["reason"])
                for field in ("sources", "source_details"):
                    for value in row.get(field, []):
                        if value not in item[field]: item[field].append(value)
                for field, value in (("web_statuses", row.get("web_status")), ("request_ids", row.get("request_id"))):
                    if value and value not in item[field]: item[field].append(value)
            except (ValueError, KeyError, TypeError):
                errors += 1
    status_file = directory / "review-status.json"
    statuses = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    for key, row in groups.items():
        row["review"] = statuses.get(key, {"status": "pending"})
        if row["review"].get("status") == "documented": row["knowledge_followup"] = "documented"
    return {"invalid_lines": errors, "questions": sorted(groups.values(), key=lambda row: (-row["count"], row["id"]))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--review-id")
    parser.add_argument("--status", choices=("pending", "documented", "dismissed"))
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    if args.review_id:
        report = summarize(directory)
        if not args.status or args.review_id not in {row["id"] for row in report["questions"]}:
            parser.error("review requires a known ID and --status")
        path = directory / "review-status.json"
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        value[args.review_id] = {"status": args.status, "note": args.note}
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = summarize(directory)
    if args.cleanup:
        result["removed_files"] = Misses(directory).cleanup()
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
