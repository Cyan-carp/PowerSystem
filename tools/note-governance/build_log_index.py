#!/usr/bin/env python3
"""Generate the monthly deployment-log timelines from `log-catalog.json`.

Why a generator: the deploy-log entry page reached 14,987 characters and the only
way to add a batch was to compress prose. The timeline now lives in per-month
pages, and this script keeps them consistent with one catalog.

Rules:

- `log-catalog.json` is the single edit location for number/title/date/batch/conclusion.
- Only files under `时间轴/` are generated wholesale, plus the block between
  `<!-- BEGIN GENERATED:timeline -->` and its END marker in the entry page.
  Everything else in the entry page stays hand-written.
- Output is deterministic: no timestamp is written, so re-running with unchanged
  input produces byte-identical files.
- Before writing, every numbered log page must appear in the catalog and every
  catalog page must exist; otherwise it stops instead of silently dropping rows.
- `log-catalog-provenance.json` freezes the pre-reorganisation table row by row.
  Its mappings are validated together with the catalog, so a renamed page cannot
  quietly break the audit trail.

Usage:
    python tools/note-governance/build_log_index.py            # 生成
    python tools/note-governance/build_log_index.py --check    # 只校验，不写

`--check` returns 0 only when every generated file already matches. A pending
file returns 1: a green CI must mean "generated output is current", not merely
"the catalog parses".
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BEGIN = "<!-- BEGIN GENERATED:timeline -->"
END = "<!-- END GENERATED:timeline -->"
RECENT = 10
PROVENANCE = Path("tools/note-governance/log-catalog-provenance.json")

HEADER = """---
tags:
  - 源网智联
  - 部署运维
  - 时间轴
status: 现行
scope: 部署运维
updated: {updated}
---

# {title}

> 本页由 `tools/note-governance/build_log_index.py` 从 `tools/note-governance/log-catalog.json` 生成，**请勿手改**。批次事实以各批次页为准，生产指针与门禁状态见 [[00-当前系统状态]]。

"""


@dataclass(frozen=True)
class Layout:
    """Every path the generator reads or writes, so tests can use a sandbox."""

    root: Path
    vault: Path
    log_dir: Path
    catalog: Path
    timeline_dir: Path
    entry_page: Path
    provenance: Path

    @classmethod
    def at(cls, root: Path) -> "Layout":
        vault = root / "notes"
        log_dir = vault / "04-系统部署发布备份与变更记录/部署与运维日志"
        return cls(
            root=root,
            vault=vault,
            log_dir=log_dir,
            catalog=root / "tools/note-governance/log-catalog.json",
            timeline_dir=log_dir / "时间轴",
            entry_page=log_dir / "00-索引·部署与运维日志.md",
            provenance=root / PROVENANCE,
        )


def load(layout: Layout) -> dict:
    return json.loads(layout.catalog.read_text(encoding="utf-8"))


def entry_link(entry: dict) -> str:
    path = entry["page"]
    target = path[:-3] if path.endswith(".md") else path
    return f"[[{target}\\|{entry['number']}]]"


def row(entry: dict, show_note: bool = True) -> str:
    date = entry["date"]
    if show_note and entry.get("date_note"):
        date = f"{date}<br><small>{entry['date_note']}</small>"
    return f"| {entry_link(entry)} | {date} | {entry['version']} | {entry['batch']} | {entry['conclusion']} |"


def validate_catalog(layout: Layout, data: dict) -> list[str]:
    problems: list[str] = []
    entries = data["entries"]
    pages = {entry["page"] for entry in entries}
    for entry in entries:
        if not (layout.vault / entry["page"]).is_file():
            problems.append(f"目录条目指向不存在的页面：{entry['page']}")
        if not entry.get("month"):
            problems.append(f"条目缺少可归属月份：{entry['number']} {entry['page']}")
    for path in layout.log_dir.rglob("*.md"):
        if path.name.startswith("00-") or layout.timeline_dir in path.parents:
            continue
        if not re.match(r"\d+-", path.name):
            continue
        rel = path.relative_to(layout.vault).as_posix()
        if rel not in pages:
            problems.append(f"日志页未登记到目录：{rel}")
    duplicates = [key for key, count in Counter((e["number"], e["page"], e["batch"]) for e in entries).items() if count > 1]
    for number, page, batch in duplicates:
        problems.append(f"目录重复条目：{number} {page} {batch}")
    collisions = defaultdict(set)
    for entry in entries:
        collisions[entry["number"]].add(entry["page"])
    for number, group in sorted(collisions.items()):
        if len(group) > 1:
            problems.append("编号重复（不同页面共用同一编号）：" + f"{number} → " + ", ".join(sorted(group)))
    return problems


def original_numbers(line: str) -> list[str]:
    """Numbers printed in the old row itself.

    A merged page can be referenced by several original numbers; the row's own
    number (leading `36 ·` or escaped alias `\\|36]]`) is the authority. Trusting
    the filename instead silently mapped the 36 row onto the 33 batch.
    """

    found: list[str] = []
    lead = re.match(r"^\|\s*(\d+)\s*·", line)
    if lead:
        found.append(lead.group(1))
    alias = re.search(r"\\\|(\d+)\]\]", line)
    if alias and alias.group(1) not in found:
        found.append(alias.group(1))
    return found


def validate_provenance(layout: Layout, entries: list[dict]) -> list[str]:
    """The frozen pre-reorganisation table must still point at real entries."""

    if not layout.provenance.is_file():
        return [f"缺少旧总表逐行映射：{layout.provenance.relative_to(layout.root).as_posix()}"]
    data = json.loads(layout.provenance.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    problems: list[str] = []
    declared = (data.get("source") or {}).get("rows")
    if declared != len(rows):
        problems.append(f"旧总表映射行数与声明不一致：声明 {declared}，实际 {len(rows)}")
    known = {(entry["number"], entry["page"]) for entry in entries}
    for item in rows:
        number = str(item.get("number", ""))
        catalog = item.get("catalog") or {}
        key = (str(catalog.get("number", "")), str(catalog.get("page", "")))
        if key not in known:
            problems.append(f"旧总表第 {item.get('row')} 行的映射已失效：{key[0]} → {key[1]}")
        elif number != key[0]:
            problems.append(f"旧总表第 {item.get('row')} 行编号与目录不一致：{number} vs {key[0]}")
        line = str(item.get("line", ""))
        if not line.startswith("|"):
            problems.append(f"旧总表第 {item.get('row')} 行缺少可对照原文")
        for printed in original_numbers(line):
            if printed != key[0]:
                problems.append(
                    f"旧总表第 {item.get('row')} 行原文编号 {printed} 映射到 {key[0]}"
                    "（同一合并页误配另一批次）"
                )
    return problems


def validate(layout: Layout, data: dict) -> list[str]:
    problems = validate_catalog(layout, data)
    if not layout.entry_page.is_file():
        problems.append(f"入口页不存在：{layout.entry_page.relative_to(layout.root).as_posix()}")
    else:
        text = layout.entry_page.read_text(encoding="utf-8")
        if BEGIN not in text or END not in text:
            problems.append(f"入口页缺少生成标记 {BEGIN} / {END}")
    problems.extend(validate_provenance(layout, data["entries"]))
    return problems


def month_pages(layout: Layout, entries: list[dict], updated: str) -> dict[Path, str]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        grouped[entry["month"]].append(entry)
    outputs: dict[Path, str] = {}
    for month, items in sorted(grouped.items()):
        items.sort(key=lambda item: (item["date"], item["number"].zfill(3), item["page"]))
        title = f"{month} · 部署批次时间轴"
        body = [HEADER.format(updated=updated, title=title)]
        body.append(f"本月共 **{len(items)}** 条目录条目（含原编号合并行与同日多次发布），按日期排列。\n")
        body.append("| 编号 | 日期 | 版本 | 批次指针 | 结论 |")
        body.append("| --- | --- | --- | --- | --- |")
        body.extend(row(item) for item in items)
        body.append("")
        body.append("## 相关")
        body.append("")
        body.append("- [[00-索引·部署批次时间轴]]")
        body.append("- [[00-索引·部署与运维日志]] · [[00-当前系统状态]]")
        outputs[layout.timeline_dir / f"{month}-部署批次时间轴.md"] = "\n".join(body) + "\n"
    return outputs


def month_index(layout: Layout, entries: list[dict], updated: str) -> str:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        grouped[entry["month"]].append(entry)
    body = [HEADER.format(updated=updated, title="部署批次时间轴 · 索引")]
    body.append("> 总入口不再承载无限追加的全量表：这里按月进入，月份页由目录自动生成。\n")
    body.append("| 月份 | 条目数 | 入口 |")
    body.append("| --- | --- | --- |")
    for month, items in sorted(grouped.items()):
        body.append(f"| {month} | {len(items)} | [[{month}-部署批次时间轴\\|{month} 时间轴]] |")
    body.append("")
    body.append("## 说明")
    body.append("")
    body.append("- 条目数包含原编号合并行（同一页面被多个原编号引用）与同日多次发布，不等于“发布次数”。")
    body.append("- 日期取自批次页或原总表；原文只写月日的，按本目录 2026 年上下文补全年份并在日期下注明。")
    body.append("- 旧总表 112 行的逐行去向见 `tools/note-governance/log-catalog-provenance.json`；编号重复、日期待核等问题在 `build_log_index.py` 的校验输出中列出，不静默合并。")
    body.append("")
    body.append("## 相关")
    body.append("")
    body.append("- [[00-索引·部署与运维日志]] · [[00-当前系统状态]]")
    return "\n".join(body) + "\n"


def generated_block(entries: list[dict]) -> str:
    ordered = sorted(entries, key=lambda item: (item["month"], item["date"], item["number"].zfill(3)), reverse=True)
    recent = ordered[:RECENT]
    lines = [BEGIN, "## 四、最近批次", "", "| 编号 | 日期 | 版本 | 批次指针 | 结论 |", "| --- | --- | --- | --- | --- |"]
    lines.extend(row(item, show_note=False) for item in recent)
    lines.extend(
        [
            "",
            "## 五、按月查看",
            "",
            "全量时间轴已按月份拆分，总入口不再逐条追加：",
            "",
            "- [[时间轴/00-索引·部署批次时间轴|部署批次时间轴索引]]（2026-09 起，按月进入）",
            "",
            "> 本块由 `build_log_index.py` 生成，只替换标记之间的内容；标记之外的人工导航不受影响。",
            END,
        ]
    )
    return "\n".join(lines)


def replace_block(text: str, block: str) -> str:
    if BEGIN not in text or END not in text:
        raise SystemExit(f"入口页缺少生成标记 {BEGIN} / {END}，已停止以免覆盖手写内容")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{block}{tail}"


def write(path: Path, content: str, check: bool) -> bool:
    current = path.read_text(encoding="utf-8") if path.is_file() else None
    if current == content:
        return False
    if not check:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return True


def run(layout: Layout, check: bool = False) -> tuple[list[str], int]:
    """(problems, changed files). Blocking problems are those not starting with WARN."""

    data = load(layout)
    problems = validate(layout, data)
    blocking = [item for item in problems if not item.startswith("编号重复")]
    if blocking:
        return problems, 0
    entries = data["entries"]
    updated = data["updated"]
    changed = 0
    for path, content in month_pages(layout, entries, updated).items():
        changed += write(path, content, check)
    changed += write(layout.timeline_dir / "00-索引·部署批次时间轴.md", month_index(layout, entries, updated), check)
    entry_text = layout.entry_page.read_text(encoding="utf-8")
    changed += write(layout.entry_page, replace_block(entry_text, generated_block(entries)), check)
    return problems, changed


def main() -> int:
    check = "--check" in sys.argv[1:]
    layout = Layout.at(ROOT)
    data = load(layout)
    entries = data["entries"]
    months = sorted({entry["month"] for entry in entries})
    problems, changed = run(layout, check)
    blocking = [item for item in problems if not item.startswith("编号重复")]
    for item in problems:
        print(("ERROR " if item in blocking else "WARN  ") + item)
    if blocking:
        print(f"FAIL: {len(blocking)} blocking problem(s)")
        return 1
    print(
        f"{'CHECK' if check else 'OK'}: {len(entries)} catalog entries, {len(months)} months "
        f"({', '.join(months)}), {changed} file(s) {'would change' if check else 'updated'}, "
        f"{len(problems) - len(blocking)} warning(s)"
    )
    if check and changed:
        print(f"FAIL: {changed} generated file(s) are stale; run without --check and commit the result")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
