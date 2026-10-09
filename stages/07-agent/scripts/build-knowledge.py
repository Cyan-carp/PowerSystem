"""Build only explicitly approved Markdown sources into the release asset."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def build(root=ROOT):
    manifest = json.loads((root / "stages/07-agent/knowledge/sources.json").read_text(encoding="utf-8"))
    chunks = []
    for source in manifest:
        path = (root / source["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or path.suffix != ".md":
            raise ValueError("source outside repository")
        text = path.read_text(encoding="utf-8-sig")
        text = re.sub(r"\A---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
        sections = list(re.finditer(r"^#{2,3} (.+)$", text, re.M))
        parent = ""
        for i, section in enumerate(sections):
            heading = section[1].strip()
            if section[0].startswith("## "):
                parent = heading
            if "相关笔记" in heading:
                break
            if source.get("sections") and not any(term in parent for term in source["sections"]):
                continue
            content = text[section.end():sections[i+1].start() if i+1 < len(sections) else len(text)].strip()
            if not content:
                continue
            # The published asset is explanatory text, not deploy commands or live addresses.
            content = re.sub(r"```.*?```", "[操作命令请查对应源码 README]", content, flags=re.S)
            content = re.sub(r"(?:https?://)?(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?", "[管理员提供的地址]", content)
            for n in range(0, len(content), 5000):
                body = content[n:n+5000]
                key = source["path"] + heading + str(n)
                chunks.append({"id": "K" + hashlib.sha256(key.encode()).hexdigest()[:20],
                    "document": source["path"], "heading": heading, "content": body,
                    "authority": source["authority"], "document_date": source.get("date"),
                    "source_kind": source.get("kind", "project_guide"),
                    "source_revision": source.get("source_version"),
                    "content_hash": hashlib.sha256(body.encode()).hexdigest()})
    version = hashlib.sha256(json.dumps(chunks, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"version": version, "chunks": chunks}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "stages/07-agent/knowledge/index.json")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    index = build()
    args.output.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"version": index["version"], "chunks": len(index["chunks"])}))
