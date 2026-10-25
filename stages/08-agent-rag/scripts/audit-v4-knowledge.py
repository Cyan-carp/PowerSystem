"""Read-only V4 knowledge audit: manifest vs disk vs active Qdrant collection.

Never imports, switches aliases or reads credentials. Content is verified by
SHA-256; file mtime is deliberately ignored. Exit code is 1 only with --strict.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

SCRIPTS = Path(__file__).resolve().parent
STAGE = SCRIPTS.parent
DEFAULT_MANIFEST = STAGE / "knowledge/sources.json"
DEFAULT_REPORT = Path("artifacts/stage8-agent-rag/audit/knowledge-audit.json")


def load_ingest():
    spec = importlib.util.spec_from_file_location("v4_knowledge_ingest", SCRIPTS / "knowledge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_origin(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return ""
        port = f":{parts.port}" if parts.port else ""
    except (TypeError, ValueError):
        return ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def audit_documents(entries, ingest, tokenizer, root):
    """Compare each enabled manifest entry with the bytes actually on disk."""
    documents, blockers, warnings = [], [], []
    for entry in entries:
        if entry.get("disabled") or ingest.EXCLUDED_MARKER in entry["path"]:
            documents.append({"document": entry["path"], "status": "skipped", "reason": "excluded"})
            continue
        path = ingest.resolve_path(entry, root)
        document = entry.get("title", entry["path"] if not Path(entry["path"]).is_absolute() else path.name)
        item = {"document": document, "path": entry["path"],
                "expected_sha256": entry.get("sha256"), "exists": path.is_file()}
        if not item["exists"]:
            item.update(status="missing", actual_sha256=None, chunks=None)
            blockers.append({"code": "document_missing", "detail": entry["path"]})
            documents.append(item); continue
        item["bytes"] = path.stat().st_size
        item["actual_sha256"] = ingest.document_sha256(path)
        if not item["expected_sha256"]:
            item["hash_status"] = "manifest_hash_missing"
            warnings.append({"code": "manifest_hash_missing", "detail": entry["path"]})
        elif item["expected_sha256"] != item["actual_sha256"]:
            item["hash_status"] = "drift"
            blockers.append({"code": "document_hash_drift", "detail": entry["path"],
                             "expected_sha256": item["expected_sha256"],
                             "actual_sha256": item["actual_sha256"]})
        else:
            item["hash_status"] = "match"
        try:
            sections = ingest.extract(path)
            has_text = any(str(text).strip() for _, text, _ in sections)
            source_rows = list(ingest.chunks(sections, tokenizer)) if tokenizer else []
        except Exception as error:
            item["chunks"] = None
            item["status"] = "unreadable"
            blockers.append({"code": "document_unreadable", "detail": entry["path"],
                             "error_class": type(error).__name__})
            documents.append(item); continue
        item["chunks"] = len(source_rows) if tokenizer else None
        item["status"] = "ok" if item["hash_status"] == "match" else "changed"
        if not has_text:
            blockers.append({"code": "no_extractable_text", "detail": entry["path"]})
        documents.append(item)
    return documents, blockers, warnings


def fetch_points(client, collection, max_points):
    points, offset = [], None
    while True:
        body = {"limit": 256, "with_payload": ["id", "document", "source_hash", "version"],
                "with_vector": False}
        if offset is not None: body["offset"] = offset
        reply = client.post(f"/collections/{collection}/points/scroll", json=body)
        reply.raise_for_status()
        result = reply.json()["result"]
        points.extend(result.get("points", []))
        offset = result.get("next_page_offset")
        if offset is None or len(points) >= max_points: break
    return points


def audit_collection(client, alias, expected_files, expected_version, max_points=200000):
    """Read-only comparison of the active collection payload with the manifest."""
    result = {"alias": alias, "resolved": None, "exists": False, "points": None,
              "payload_versions": [], "documents": {}, "diff": {}}
    blockers = []
    aliases = client.get("/aliases")
    aliases.raise_for_status()
    resolved = next((row["collection_name"] for row in aliases.json()["result"]["aliases"]
                     if row["alias_name"] == alias), None)
    if not resolved:
        blockers.append({"code": "collection_alias_missing", "detail": alias})
        return result, blockers
    result["resolved"] = resolved
    count = client.post(f"/collections/{resolved}/points/count", json={"exact": True})
    count.raise_for_status()
    result["points"] = count.json()["result"]["count"]
    result["exists"] = True
    points = fetch_points(client, resolved, max_points)
    if len(points) != result['points']:
        blockers.append({'code':'active_scan_incomplete','detail':resolved})
    versions, documents = set(), {}
    for point in points:
        payload = point.get("payload") or {}
        document = payload.get("document")
        if not isinstance(document, str): continue
        bucket = documents.setdefault(document, {"points": 0, "hashes": set(), "versions": set()})
        bucket["points"] += 1
        if isinstance(payload.get("source_hash"), str): bucket["hashes"].add(payload["source_hash"])
        if isinstance(payload.get("version"), str):
            bucket["versions"].add(payload["version"]); versions.add(payload["version"])
    result["payload_versions"] = sorted(versions)
    result["documents"] = {name: {"points": row["points"], "hashes": sorted(row["hashes"]),
                                  "versions": sorted(row["versions"])} for name, row in documents.items()}
    expected_names = {row["document"] for row in expected_files}
    missing = sorted(expected_names - set(documents))
    extra = sorted(set(documents) - expected_names)
    hash_drift, version_mismatch, chunk_mismatch = [], [], []
    for row in expected_files:
        bucket = documents.get(row["document"])
        if not bucket: continue
        if bucket['hashes'] != {row['sha256']}:
            hash_drift.append(row["document"])
        if expected_version and bucket['versions'] != {expected_version}:
            version_mismatch.append(row["document"])
        if row.get("chunks") is not None and bucket["points"] != row["chunks"]:
            chunk_mismatch.append({"document": row["document"], "expected": row["chunks"],
                                   "actual": bucket["points"]})
    result["diff"] = {"missing_documents": missing, "extra_documents": extra,
                      "hash_drift": hash_drift, "version_mismatch": version_mismatch,
                      "chunk_count_mismatch": chunk_mismatch}
    for name in missing:
        blockers.append({"code": "active_document_missing", "detail": name})
    for name in hash_drift:
        blockers.append({"code": "active_hash_drift", "detail": name})
    for name in version_mismatch:
        blockers.append({"code": "active_version_mismatch", "detail": name})
    for row in chunk_mismatch:
        blockers.append({"code": "active_chunk_count_mismatch", "detail": row["document"]})
    for name in extra:
        blockers.append({"code": "active_document_unexpected", "detail": name})
    check = client.get('/aliases')
    check.raise_for_status()
    if not any(a['alias_name']==alias and a['collection_name']==resolved for a in check.json()['result']['aliases']):
        blockers.append({'code':'active_alias_changed','detail':alias})
    return result, blockers


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--root", type=Path, default=None)
    parser.add_argument("--qdrant-url", default=os.getenv("RAG_QDRANT_URL", "http://127.0.0.1:16333"))
    parser.add_argument("--collection", default=os.getenv("RAG_COLLECTION", "powersystem_rag_active"))
    parser.add_argument("--embedding-path", default=os.getenv("RAG_EMBEDDING_PATH", "intfloat/multilingual-e5-small"))
    parser.add_argument("--skip-qdrant", action="store_true")
    parser.add_argument("--max-points", type=int, default=200000)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)

    ingest = load_ingest()
    entries = ingest.manifest_entries(args.manifest)
    chunking = {"kind": "unavailable", "authoritative": False}
    tokenizer = None
    if Path(args.embedding_path).exists():
        try:
            embedding = ingest.Embedding(args.embedding_path)
            tokenizer = embedding.load().tokenizer
            chunking = {"kind": "sentence-transformers", "authoritative": True,
                        "path": args.embedding_path}
        except Exception as error:
            chunking = {"kind": "unavailable", "authoritative": False,
                        "reason": type(error).__name__, "path": args.embedding_path}
    else:
        chunking = {"kind": "unavailable", "authoritative": False,
                    "reason": "embedding_model_not_found", "path": args.embedding_path}

    documents, blockers, warnings = audit_documents(entries, ingest, tokenizer, args.root)
    expected_files = []
    for row in documents:
        if row["status"] in ("skipped", "missing", "unreadable"): continue
        expected_files.append({"document": row["document"], "sha256": row.get("actual_sha256"),
                               "chunks": row.get("chunks") if chunking["authoritative"] else None})
    expected_version = None
    if tokenizer and not any(row["status"] in ("missing", "changed", "unreadable") for row in documents):
        _, files, _, _, _ = ingest.collect(entries, tokenizer, root=args.root, verify_manifest=False)
        expected_version = hashlib.sha256(
            json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    active = None
    if not args.skip_qdrant:
        if not chunking['authoritative']:
            blockers.append({'code':'chunking_unverified','detail':chunking.get('reason','unavailable')})
        origin = safe_origin(args.qdrant_url)
        if not origin:
            blockers.append({"code": "qdrant_url_invalid", "detail": "configured origin rejected"})
        else:
            try:
                with httpx.Client(base_url=origin, timeout=30, trust_env=False) as client:
                    active, found = audit_collection(client, args.collection, expected_files,
                                                     expected_version, args.max_points)
                    blockers.extend(found)
            except httpx.HTTPError as error:
                blockers.append({"code": "qdrant_unavailable", "detail": type(error).__name__})
    else:
        warnings.append({"code": "qdrant_skipped", "detail": "read-only document audit only"})

    digest = ingest.manifest_sha256(entries)
    report = {"status": "blocked" if blockers else "ok", "read_only": True,
              "evidence": "content_sha256 (mtime ignored)",
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "manifest": {"path": str(args.manifest), "sha256": digest,
                           "version": "m-" + digest[:12], "entries": len(entries)},
              "chunking": chunking, "documents": documents,
              "expected_version": expected_version,
              "active_collection": active, "blockers": blockers, "warnings": warnings}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "manifest_version": report["manifest"]["version"],
                      "expected_version": expected_version,
                      "blockers": [row["code"] for row in blockers],
                      "report": str(args.report)}))
    return 1 if (args.strict and blockers) else 0


if __name__ == "__main__":
    sys.exit(main())
