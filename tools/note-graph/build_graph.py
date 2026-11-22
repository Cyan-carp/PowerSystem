#!/usr/bin/env python3
"""Build the static note graph from the isolated ``notes`` Vault."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "notes"
SITE = ROOT / "tools/note-graph/site"
ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".mmd", ".pdf"}

# 复用检查器的提取、定位与锚点判定：另写一套扫描就会出现
# “图谱说断链、检查器说通过”，或者目标页存在就把不存在的章节当成正常。
sys.path.insert(0, str(ROOT / "tools/note-governance"))
import note_links as nl  # noqa: E402


def parse_frontmatter(text: str) -> tuple[list[str], str]:
    tags: list[str] = []
    if not text.startswith("---\n"):
        return tags, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return tags, text
    header = text[4:end].splitlines()
    in_tags = False
    for line in header:
        st = line.strip()
        if st.startswith("tags:"):
            inline = st[5:].strip()
            if inline.startswith("[") and inline.endswith("]"):
                tags.extend(t.strip().strip("\"'") for t in inline[1:-1].split(",") if t.strip())
                in_tags = False
            else:
                in_tags = True
            continue
        if in_tags and re.match(r"^\s+-\s+", line):
            tags.append(re.sub(r"^\s+-\s+", "", line).strip())
            continue
        if line and not line.startswith((" ", "\t")):
            in_tags = False
    return tags, text[end + 5 :]


def title_of(path: Path, body: str) -> str:
    match = re.search(r"^#\s+(.+)$", body, flags=re.M)
    return match.group(1).strip() if match else path.stem


def resolve_note(target: str, source: Path, by_stem: dict[str, list[Path]]) -> Path | None:
    if not target:
        return None
    result = nl.resolve(target, source, by_stem, vault=VAULT, kind="wikilink", notes_only=True, root=ROOT)
    return result.path.resolve() if result.status == "ok" and result.path else None


def source_digest(paths: list[Path]) -> str:
    """Digest of the actual inputs, not of the derived payload.

    Hashing the payload meant an equal-length edit to a note's body could leave
    every statistic unchanged, so the file still claimed the inputs were the same.
    """

    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def make_tree(nodes: list[dict[str, object]]) -> dict[str, object]:
    """Directory tree for the mind map.

    Leaves only carry what the renderer reads; ``tags``/``top``/``id`` are
    recoverable from ``nodes`` through ``path`` and would otherwise duplicate
    roughly 100 KB of the payload.
    """
    root: dict[str, object] = {"name": "notes", "path": "notes", "type": "dir", "children": {}}
    for node in nodes:
        parts = Path(str(node["path"])).parts
        current = root
        for part in parts[1:-1]:
            children = current["children"]
            assert isinstance(children, dict)
            current = children.setdefault(
                part,
                {"name": part, "path": "", "type": "dir", "children": {}},
            )
        children = current["children"]
        assert isinstance(children, dict)
        children[parts[-1]] = {
            "name": str(node["name"]),
            "path": str(node["path"]),
            "type": "note",
            "deg": node["deg"],
            "isIndex": node["isIndex"],
            "count": 1,
        }

    def finish(item: dict[str, object], path: str = "notes") -> dict[str, object]:
        children = item.get("children")
        if not isinstance(children, dict):
            return item
        rendered = []
        for name, child in sorted(children.items(), key=lambda pair: pair[0]):
            child_path = f"{path}/{name}"
            child["path"] = child_path
            rendered.append(finish(child, child_path))
        item["children"] = rendered
        item["count"] = sum(int(child.get("count", 1)) for child in rendered)
        return item

    return finish(root)


def dumps(data: dict[str, object]) -> str:
    """Compact JSON that keeps one array element per line.

    A single 600 KB line makes the generated file unopenable in most editors
    (and slow to diff) even though browsers parse it fine, so collections of
    records are emitted one per line while scalars stay compact.
    """
    parts: list[str] = []
    for key, value in data.items():
        if isinstance(value, list) and value and not isinstance(value[0], (str, int, float)):
            body = ",\n".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in value
            )
            parts.append(f'"{key}":[\n{body}\n]')
        else:
            parts.append(f'"{key}":' + json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return "{\n" + ",\n".join(parts) + "\n}"


def main() -> None:
    paths = sorted(path.resolve() for path in VAULT.rglob("*.md"))
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in paths:
        by_stem[path.stem.casefold()].append(path)

    records: dict[Path, dict[str, object]] = {}
    pages: dict[Path, str] = {}
    top_groups: dict[str, list[Path]] = defaultdict(list)
    raw_links = 0
    notes_with_tags = 0
    total_chars = 0
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        pages[path] = text
        tags, body = parse_frontmatter(text)
        rel = path.relative_to(ROOT).as_posix()
        vault_rel = path.relative_to(VAULT)
        top = vault_rel.parts[0] if len(vault_rel.parts) > 1 else "(根)"
        top_groups[top].append(path)
        notes_with_tags += bool(tags)
        total_chars += len(text)
        records[path] = {
            "id": rel,
            "path": rel,
            "name": path.stem,
            "title": title_of(path, body),
            "dir": path.parent.relative_to(ROOT).as_posix(),
            "top": top,
            "depth": len(vault_rel.parts) - 1,
            "isIndex": path.name.startswith("00-"),
            "tags": tags,
            "h2": len(re.findall(r"^##\s+", body, flags=re.M)),
            "chars": len(text),
            "deg": 0,
            "in": 0,
            "out": 0,
        }

    edges_seen: set[tuple[str, str, str]] = set()
    broken: list[dict[str, str]] = []

    def note_broken(source: Path, target: str, reason: str) -> None:
        broken.append(
            {
                "source": source.relative_to(ROOT).as_posix(),
                "target": target,
                "reason": reason,
            }
        )

    for source in paths:
        text = pages[source]
        # 图谱只画 Obsidian 双链；Markdown 链接由检查器负责，避免同一根线被两种语法重复计权。
        for link in nl.iter_links(text):
            if link.kind != "wikilink":
                continue
            raw_links += 1
            if not link.target:
                if link.subpath and nl.anchor_status(text, link.subpath, "obsidian") == "missing":
                    note_broken(source, "#" + link.subpath, "missing-anchor")
                continue
            target = resolve_note(link.target, source, by_stem)
            if target is None:
                if Path(link.target).suffix.casefold() in ATTACHMENT_SUFFIXES:
                    continue
                note_broken(source, link.target, "missing-target")
                continue
            if target not in records:
                continue
            if link.subpath:
                # broken=0 只有在校验过章节时才有意义；目标页存在不等于章节存在。
                result = nl.anchor_status(pages.get(target, ""), link.subpath, "obsidian")
                if result == "missing":
                    note_broken(source, f"{link.target}#{link.subpath}", "missing-anchor")
            source_id = str(records[source]["id"])
            target_id = str(records[target]["id"])
            if link.subpath:
                edge_type = "anchor"
            elif bool(records[source]["isIndex"]):
                edge_type = "index"
            elif bool(records[target]["isIndex"]):
                edge_type = "backref"
            elif records[source]["dir"] == records[target]["dir"]:
                edge_type = "sibling"
            elif str(records[target]["path"]).startswith(
                ("notes/02-工程规范边界与验收治理/", "notes/03-平台使用运维与二次开发手册/")
            ):
                edge_type = "govern"
            else:
                edge_type = "cross"
            edges_seen.add((source_id, target_id, edge_type))

    edges = [{"s": source, "t": target, "type": edge_type} for source, target, edge_type in sorted(edges_seen)]
    incoming = Counter(edge["t"] for edge in edges)
    outgoing = Counter(edge["s"] for edge in edges)
    for record in records.values():
        node_id = str(record["id"])
        record["in"] = incoming[node_id]
        record["out"] = outgoing[node_id]
        record["deg"] = incoming[node_id] + outgoing[node_id]

    # The browser performs the sole deterministic layout.  Initial coordinates
    # are deliberately neutral so the data generator does not retain the former
    # path-hash scatter algorithm as a second, contradictory layout contract.
    for record in records.values():
        record["x"] = 0
        record["y"] = 0

    nodes = [records[path] for path in paths]
    node_pos = {str(record["path"]): index for index, record in enumerate(nodes)}
    top_of = {str(record["path"]): str(record["top"]) for record in nodes}

    # Links are stored as [sourceIndex, targetIndex, typeIndex]. Repeating the
    # 140-character absolute path twice per link cost ~750 KB of the payload;
    # indices plus one shared type table bring that down by an order of
    # magnitude without changing what the renderer can express.
    edge_types = Counter(str(edge["type"]) for edge in edges)
    link_types = [name for name, _ in edge_types.most_common()]
    type_pos = {name: index for index, name in enumerate(link_types)}
    compact_edges = [
        [node_pos[str(edge["s"])], node_pos[str(edge["t"])], type_pos[str(edge["type"])]]
        for edge in edges
    ]

    top_counts = Counter(str(node["top"]) for node in nodes)
    tag_counts = Counter(tag for node in nodes for tag in node["tags"])
    dir_counts = Counter(str(node["dir"]) for node in nodes)
    matrix_counts = Counter((top_of[str(edge["s"])], top_of[str(edge["t"])]) for edge in edges)

    tree = make_tree(nodes)
    for record in nodes:
        record.pop("id", None)  # identical to "path" for every note

    payload = {
        "stats": {
            "notes": len(nodes),
            "rawWikilinks": raw_links,
            "edges": len(edges),
            "broken": len(broken),
            "brokenTargets": sum(1 for item in broken if item["reason"] == "missing-target"),
            "brokenAnchors": sum(1 for item in broken if item["reason"] == "missing-anchor"),
            "tags": len(tag_counts),
            "linkTypes": edge_types.most_common(),
            "topCounts": top_counts.most_common(),
            "notesWithTags": notes_with_tags,
            "chars": total_chars,
        },
        "nodes": nodes,
        "edges": compact_edges,
        "tree": tree,
        "matrix": [{"s": source, "t": target, "n": count} for (source, target), count in matrix_counts.most_common()],
        "topTags": tag_counts.most_common(30),
        "broken": broken,
        "dirs": [{"path": directory, "count": count} for directory, count in dir_counts.most_common()],
        "hubs": [
            {"id": node["path"], "deg": node["deg"], "in": node["in"], "out": node["out"]}
            for node in sorted(nodes, key=lambda item: int(item["deg"]), reverse=True)[:20]
        ],
        # Filled from final node coordinates by rebuildClusters() in template.html.
        "clusters": [],
    }

    # 输入摘要决定 generated 是否刷新：同一份笔记重复生成必须字节一致，
    # 否则每次 build 都会改写 index.html，CI 与人工 diff 都失去意义。
    # 摘要按笔记路径与字节内容计算，不用派生 payload：后者在等长正文改动时不变。
    digest = source_digest(paths)
    previous: dict[str, object] = {}
    if (SITE / "graph-data.json").is_file():
        try:
            previous = json.loads((SITE / "graph-data.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    if previous.get("inputDigest") == digest and isinstance(previous.get("generated"), str):
        generated = str(previous["generated"])
    else:
        generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    data = {
        "generated": generated,
        "generatedNote": "生成时间（UTC，ISO 8601）。输入摘要按笔记路径与字节内容计算，未变时沿用上次时间；不是生产更新时间。",
        "inputDigest": digest,
        **payload,
    }
    compact = dumps(data)
    (SITE / "graph-data.json").write_text(compact + "\n", encoding="utf-8", newline="\n")
    template = (SITE / "template.html").read_text(encoding="utf-8")
    if "__DATA__" not in template:
        raise RuntimeError("template.html is missing __DATA__ placeholder")
    (SITE / "index.html").write_text(template.replace("__DATA__", compact), encoding="utf-8", newline="\n")
    print(f"generated graph: notes={len(nodes)} edges={len(edges)} broken={len(broken)}")


if __name__ == "__main__":
    main()
