"""Build a fresh vector collection, then atomically switch the active alias."""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_agent.config import Config
from rag_agent.knowledge import Embedding, point_id

ROOT = Path(os.environ["RAG_DOCUMENT_ROOT"]) if "RAG_DOCUMENT_ROOT" in os.environ else Path(__file__).resolve().parents[3]
DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "knowledge/sources.json"
EXCLUDED_MARKER = "废案-旧智能体方案"


class CharTokenizer:
    """Dry-run only fallback so the manifest can be checked without the model."""

    def encode(self, text, **kwargs):
        return list(text)

    def decode(self, tokens):
        return "".join(tokens)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def manifest_sha256(entries):
    return hashlib.sha256(canonical_json(entries).encode()).hexdigest()


def document_sha256(path):
    path = Path(path)
    raw = path.read_bytes()
    # Deployment normalizes text to LF. Pin the same UTF-8 text on Windows and
    # Linux while retaining byte hashes for binary sources.
    if path.suffix.lower() in ('.md', '.txt'):
        raw = raw.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def manifest_entries(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("knowledge_manifest_must_be_a_list")
    return payload


def resolve_path(entry, root=None):
    path = Path(entry["path"])
    return path if path.is_absolute() else Path(root or ROOT) / path


def extract(path):
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8-sig")
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
        sections, heading, block = [], path.stem, []
        for line in text.splitlines():
            if line.startswith("#") and re.match(r"^#{1,6}\s", line):
                if block: sections.append((heading, "\n".join(block), None))
                heading, block = line.lstrip("# "), []
            else: block.append(line)
        if block: sections.append((heading, "\n".join(block), None))
        return sections
    if suffix == ".docx":
        from docx import Document
        from docx.text.paragraph import Paragraph
        from docx.table import Table
        doc = Document(path)
        sections, heading, block = [], path.stem, []
        for item in doc.element.body:
            if item.tag.endswith("}p"):
                paragraph = Paragraph(item, doc)
                if paragraph.style.name.startswith("Heading"):
                    if block: sections.append((heading, "\n".join(block), None))
                    heading, block = paragraph.text, []
                elif paragraph.text.strip(): block.append(paragraph.text)
            elif item.tag.endswith("}tbl"):
                block.extend(" | ".join(cell.text for cell in row.cells) for row in Table(item, doc).rows)
        if block: sections.append((heading, "\n".join(block), None))
        return sections
    if suffix == ".pdf":
        from pypdf import PdfReader
        return [(path.stem, page.extract_text() or "", i + 1) for i, page in enumerate(PdfReader(path).pages)]
    raise ValueError("unsupported document format")


def chunks(sections, tokenizer):
    for heading, text, page in sections:
        if not text.strip(): continue
        # Paragraph boundaries are preferred, with token windows for long paragraphs.
        head = tokenizer.encode(heading + "\n", add_special_tokens=False)[:80]
        budget = 400 - len(head)
        paragraphs = re.split(r"\n\s*\n", text)
        token_ids = []
        for paragraph in paragraphs:
            ids = tokenizer.encode(paragraph + "\n", add_special_tokens=False, verbose=False)
            if token_ids and len(token_ids) + len(ids) > budget:
                yield heading, tokenizer.decode(head + token_ids), page
                token_ids = token_ids[-60:]
            while len(token_ids) + len(ids) > budget:
                take = budget - len(token_ids)
                segment = token_ids + ids[:take]
                yield heading, tokenizer.decode(head + segment), page
                ids = ids[take:]
                token_ids = segment[-60:]
            token_ids.extend(ids)
        if token_ids: yield heading, tokenizer.decode(head + token_ids), page


def collect(entries, tokenizer, root=None, delete_document="", verify_manifest=True):
    """Return rows, document summary, skips, chunk index and blockers.

    Content hashes are always recomputed from disk; file mtime is never used.
    A manifest hash mismatch blocks a real import instead of silently
    republishing different bytes under the same manifest version.
    """
    rows, files, skipped, chunk_index, blockers = [], [], [], [], []
    for entry in entries:
        if entry.get("disabled") or entry["path"] == delete_document: continue
        if EXCLUDED_MARKER in entry["path"]:
            skipped.append({"document": entry["path"], "reason": "废案不自动入库"}); continue
        path = resolve_path(entry, root)
        if not path.is_file():
            blockers.append({"code": "document_missing", "document": entry["path"]}); continue
        digest = document_sha256(path)
        expected = entry.get("sha256")
        if expected and expected != digest:
            blockers.append({"code": "document_hash_drift", "document": entry["path"],
                             "expected_sha256": expected, "actual_sha256": digest})
            if verify_manifest: continue
        extracted = extract(path)
        document = entry.get("title", entry["path"] if not Path(entry["path"]).is_absolute() else path.name)
        source_rows = list(chunks(extracted, tokenizer))
        if not source_rows:
            skipped.append({"document": document, "reason": "未提取到文本，扫描PDF需要另行OCR"}); continue
        applicability = entry.get('applicability', {'source_type':'project_summary'})
        if not isinstance(applicability, dict): raise ValueError('invalid_applicability_metadata')
        metadata_version = json.dumps(applicability, ensure_ascii=False, sort_keys=True, allow_nan=False)
        files.append({"document": document, "sha256": digest, "chunks": len(source_rows), 'applicability':applicability})
        for i, (heading, text, page) in enumerate(source_rows):
            identifier = "K" + hashlib.sha256((document + digest + metadata_version + str(i)).encode()).hexdigest()[:20]
            rows.append({"id": identifier, "document": document, "heading": heading,
                         "content": text, "summary": text[:200], "page": page, "source_hash": digest, 'applicability':applicability})
            chunk_index.append({"id": identifier, "document": document, "page": page,
                                "chars": len(text),
                                "content_sha256": hashlib.sha256(text.encode()).hexdigest()})
    return rows, files, skipped, chunk_index, blockers


def load_config(dry_run):
    """Real imports keep the production config; dry-run works without secrets."""
    if not dry_run:
        return Config.load()
    return Config(model=os.getenv("AGENT_LLM_MODEL", "dry-run-model"),
            llm_url=os.getenv("AGENT_LLM_BASE_URL", "http://127.0.0.1:1").rstrip("/"),
            llm_key="", service_token="dry-run", database_url="", redis_url="",
            embedding_path=os.getenv("RAG_EMBEDDING_PATH", "intfloat/multilingual-e5-small"),
            collection=os.getenv("RAG_COLLECTION", "powersystem_rag_active"),
            audit_dir=Path(os.getenv("RAG_AUDIT_DIR", "artifacts/stage8-agent-rag/audit")))


def load_tokenizer(cfg, dry_run):
    path = Path(cfg.embedding_path)
    if dry_run and not path.exists():
        return CharTokenizer(), None, {"kind": "char-fallback", "authoritative": False,
                                       "reason": "embedding_model_not_found", "path": cfg.embedding_path}
    embedding = Embedding(cfg.embedding_path)
    try:
        tokenizer = embedding.load().tokenizer
    except (ImportError, OSError) as error:
        if not dry_run: raise
        return CharTokenizer(), None, {'kind':'char-fallback','authoritative':False,'reason':type(error).__name__}
    return tokenizer, embedding, {"kind": "sentence-transformers",
                                                   "authoritative": True, "path": cfg.embedding_path}


def build_report(entries, rows, files, skipped, chunk_index, blockers, cfg, chunking, manifest_path):
    version = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    for row in rows: row["version"] = version
    digest = manifest_sha256(entries)
    enabled = sum(1 for entry in entries
                  if not entry.get("disabled") and EXCLUDED_MARKER not in entry["path"])
    return {"version": version,
        "manifest": {"path": str(manifest_path), "sha256": digest, "version": "m-" + digest[:12],
                     "entries": len(entries), "enabled": enabled},
        "import_summary": {"documents": len(files), "chunks": len(rows), "skipped": len(skipped),
                           "chunks_digest": hashlib.sha256(canonical_json(chunk_index).encode()).hexdigest(),
                           "chunking": chunking},
        "embedding": cfg.embedding_path, "dimension": 384,
        "documents": files, "skipped": skipped, "chunk_index": chunk_index,
        "blockers": blockers, "chunks": len(rows), "collection": None,
        "collection_prefix": "powersystem_rag_" + version[:12],
        "importable": not blockers,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "dry_run"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--delete-document", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.dry_run)
    tokenizer, embedding, chunking = load_tokenizer(cfg, args.dry_run)
    entries = manifest_entries(args.manifest)
    rows, files, skipped, chunk_index, blockers = collect(entries, tokenizer,
        delete_document=args.delete_document, verify_manifest=not args.dry_run)
    if blockers and not args.dry_run:
        raise ValueError("knowledge_manifest_blocked: " + ",".join(sorted({b["code"] for b in blockers})))
    report = build_report(entries, rows, files, skipped, chunk_index, blockers, cfg, chunking, args.manifest)
    if not args.dry_run:
        if not rows: raise ValueError("refusing to replace a knowledge collection with no extracted text")
        if embedding is None: raise ValueError("embedding_required_for_publish")
        collection = report["collection_prefix"] + "_" + uuid4().hex[:8]
        report["collection"] = collection
        with httpx.Client(base_url=cfg.qdrant_url, timeout=120, trust_env=False) as client:
            old = client.get("/aliases"); old.raise_for_status()
            previous = next((a["collection_name"] for a in old.json()["result"]["aliases"] if a["alias_name"] == cfg.collection), None)
            reply = client.put(f"/collections/{collection}", json={"vectors": {"size": 384, "distance": "Cosine"}}); reply.raise_for_status()
            for start in range(0, len(rows), 32):
                batch = rows[start:start + 32]
                vectors = embedding.encode([r["content"] for r in batch])
                reply = client.put(f"/collections/{collection}/points?wait=true", json={"points": [
                    {"id": point_id(row["id"]), "vector": vector, "payload": row} for row, vector in zip(batch, vectors)]})
                reply.raise_for_status()
            count = client.post(f"/collections/{collection}/points/count", json={"exact": True}); count.raise_for_status()
            if count.json()["result"]["count"] != len(rows): raise ValueError("vector count mismatch")
            # Smoke-test query before alias mutation; failed builds leave the old alias intact.
            probe = client.post(f"/collections/{collection}/points/query", json={"query": embedding.encode(["平台设备告警"], True)[0], "limit": 1})
            probe.raise_for_status()
            actions = ([{"delete_alias": {"alias_name": cfg.collection}}] if previous else [])
            actions.append({"create_alias": {"collection_name": collection, "alias_name": cfg.collection}})
            reply = client.post("/collections/aliases", json={"actions": actions}); reply.raise_for_status()
            report.update(status="published", previous_collection=previous)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "chunks": len(rows), "skipped": len(skipped),
                      "version": report["version"], "manifest_version": report["manifest"]["version"],
                      "blockers": [b["code"] for b in blockers]}))


if __name__ == "__main__":
    import sys
    try:
        main()
    except Exception as error:
        # A failed command must not leave an older published report looking current.
        if "--report" in sys.argv:
            target = Path(sys.argv[sys.argv.index("--report") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"status":"failed", "error_class":type(error).__name__,
                "created_at":datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
        print("Knowledge import failed:", type(error).__name__)
        raise SystemExit(1)
