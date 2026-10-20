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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path(__file__).resolve().parents[1] / "knowledge/sources.json")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--delete-document", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cfg = Config.load()
    embedding = Embedding(cfg.embedding_path)
    tokenizer = embedding.load().tokenizer
    rows, files, skipped = [], [], []
    for entry in json.loads(args.manifest.read_text(encoding="utf-8-sig")):
        if entry.get("disabled") or entry["path"] == args.delete_document: continue
        if "废案-旧智能体方案" in entry["path"]:
            skipped.append({"document": entry["path"], "reason": "废案不自动入库"}); continue
        path = Path(entry["path"])
        if not path.is_absolute(): path = ROOT / path
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
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
    version = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    for row in rows: row["version"] = version
    collection = "powersystem_rag_" + version[:12] + "_" + uuid4().hex[:8]
    report = {"version": version, "embedding": cfg.embedding_path, "dimension": 384,
        "documents": files, "skipped": skipped, "chunks": len(rows), "collection": collection,
        "created_at": datetime.now(timezone.utc).isoformat(), "status": "dry_run"}
    if not args.dry_run:
        if not rows: raise ValueError("refusing to replace a knowledge collection with no extracted text")
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
    print(json.dumps({"status": report["status"], "chunks": len(rows), "skipped": len(skipped), "version": version}))


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
