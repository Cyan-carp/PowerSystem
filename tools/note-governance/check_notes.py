#!/usr/bin/env python3
"""Validate the isolated Obsidian vault and its audited workspace links.

Usage:
    python tools/note-governance/check_notes.py        # 本地严格模式
    python tools/note-governance/check_notes.py --ci   # CI 模式

CI 模式放宽两类“按设计不入库”的目标：`artifacts/` 下的私有证据在全新克隆中
必然缺失，因此指向它的 Markdown 链接与 manifest 条目不再判为缺失错误；
仓库内的文件、双链、元数据与代理页仍按严格口径校验。
"""

from __future__ import annotations

import json
import re
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[2]
VAULT = ROOT / "notes"
MANIFEST = ROOT / "tools/note-governance/workspace-links.json"
CHECKLIST = VAULT / "02-工程规范边界与验收治理/项目验收与收尾清单.md"
VALID_STATUS = {"现行", "历史", "废弃", "草稿"}
VALID_SCOPE = {
    "第一版",
    "第二版-问答",
    "第二版-多智能体",
    "补充业务",
    "跨版本",
    "部署运维",
    "资产",
}
WIKILINK = re.compile(r"!?(?:\[\[)([^\]]+)(?:\]\])")
MARKDOWN_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip("'\"")
    return result


def strip_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    return re.sub(r"`[^`\n]*`", "", text)


def candidate_paths(source: Path, target: str) -> list[Path]:
    target = unquote(target.strip().strip("<>")).replace("\\", "/")
    if not target:
        return []
    raw = Path(target)
    bases = [VAULT / raw, source.parent / raw]
    candidates: list[Path] = []
    for base in bases:
        candidates.append(base)
        if base.suffix.casefold() != ".md":
            candidates.append(base.with_suffix(".md"))
            candidates.append(Path(str(base) + ".md"))
        if not base.suffix:
            candidates.append(base / "README.md")
    return candidates


def resolves(source: Path, target: str, by_stem: dict[str, list[Path]]) -> bool:
    # Markdown 表格里的 wikilink 必须写成 [[路径\|别名]]，反斜杠只是转义，
    # 不参与路径。Linux 上 Path 不把 "\\" 当分隔符，必须先还原再切别名。
    target = target.replace("\\|", "|")
    target = target.split("|", 1)[0].split("#", 1)[0].strip()
    if not target:
        return True
    lowered = target.lower()
    if lowered.startswith(("http://", "https://", "file://", "mailto:", "obsidian://")):
        return True
    for candidate in candidate_paths(source, target):
        if candidate.exists():
            return True
    if "/" not in target and "\\" not in target:
        key = Path(target).stem.casefold()
        return len(by_stem.get(key, [])) == 1
    return False


def git_tracked(path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


def main() -> int:
    ci_mode = "--ci" in sys.argv[1:]
    errors: list[str] = []
    notes = sorted(VAULT.rglob("*.md"))
    all_files = [path for path in VAULT.rglob("*") if path.is_file()]
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in all_files:
        by_stem[path.stem.casefold()].append(path)

    duplicate_names = [name for name, count in Counter(p.name.casefold() for p in notes).items() if count > 1]
    if duplicate_names:
        errors.append("duplicate Markdown basenames: " + ", ".join(sorted(duplicate_names)))

    for directory in (path for path in VAULT.rglob("*") if path.is_dir()):
        direct_notes = list(directory.glob("*.md"))
        if len(direct_notes) <= 1 or any(path.name.startswith("00-") for path in direct_notes):
            continue
        sibling_index = directory.with_suffix(".md")
        if not sibling_index.is_file():
            errors.append(f"multi-note directory lacks index: {directory.relative_to(VAULT).as_posix()}")

    for base in (VAULT, ROOT / "stages", ROOT / "deploy", ROOT / "tools"):
        for directory in base.rglob("*"):
            if directory.is_dir() and directory.name.startswith("开发阶段"):
                errors.append(f"legacy development-stage directory: {directory.relative_to(ROOT)}")

    for path in notes:
        text = path.read_text(encoding="utf-8-sig")
        meta = frontmatter(text)
        rel = path.relative_to(ROOT).as_posix()
        if meta.get("status") not in VALID_STATUS:
            errors.append(f"invalid or missing status: {rel}")
        if meta.get("scope") not in VALID_SCOPE:
            errors.append(f"invalid or missing scope: {rel}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.get("updated", "")):
            errors.append(f"invalid or missing updated: {rel}")
        if len(text) > 15_000:
            errors.append(f"oversized note ({len(text)} chars): {rel}")

        body = strip_code(text)
        for raw in WIKILINK.findall(body):
            target = raw.split("|", 1)[0]
            if target.startswith(("stages/", "deploy/", "artifacts/", "tools/")):
                errors.append(f"direct workspace wikilink in {rel}: [[{raw}]]")
            if not resolves(path, target, by_stem):
                errors.append(f"broken wikilink in {rel}: [[{raw}]]")

        for raw in MARKDOWN_LINK.findall(body):
            target = raw.split("#", 1)[0].strip()
            if not target or target.startswith(("#", "http://", "https://", "file://", "mailto:", "data:")):
                continue
            if any(mark in target for mark in ("${", "{{", "<", ">")):
                continue
            if not resolves(path, target, by_stem):
                if ci_mode and "artifacts/" in target.replace("\\", "/"):
                    continue
                errors.append(f"broken Markdown link in {rel}: ({raw})")

    if not CHECKLIST.exists():
        errors.append("acceptance checklist is missing")
    else:
        for number, line in enumerate(CHECKLIST.read_text(encoding="utf-8-sig").splitlines(), 1):
            if re.match(r"^\s*- \[ \]", line) and "来源：" not in line:
                errors.append(f"unchecked checklist item lacks source at line {number}")

    if not MANIFEST.exists():
        errors.append("workspace link manifest is missing")
    else:
        entries = json.loads(MANIFEST.read_text(encoding="utf-8"))
        proxies = VAULT / "06-代码工作区导航与链接映射"
        for entry in entries:
            workspace_path = entry["workspace_path"]
            target = ROOT / workspace_path
            if not target.exists():
                if entry.get("tracked") or not ci_mode:
                    errors.append(f"workspace target is missing: {workspace_path}")
                elif not (proxies / entry["proxy"]).is_file():
                    errors.append(f"workspace proxy is missing: {entry['proxy']}")
                continue
            expected_relative = Path(os.path.relpath(target, proxies)).as_posix()
            relative_path = entry.get("relative_path")
            if relative_path != expected_relative:
                errors.append(f"stale relative_path: {workspace_path}")
            if not relative_path or Path(str(relative_path)).is_absolute() or "://" in str(relative_path):
                errors.append(f"workspace path is not relative: {workspace_path}")
            elif (proxies / str(relative_path)).resolve() != target.resolve():
                errors.append(f"relative workspace path resolves incorrectly: {workspace_path}")
            # Git tracks files, not directory objects.  A directory proxy may
            # resolve locally but intentionally has no file-level GitHub URL.
            tracked = target.is_file() and git_tracked(workspace_path)
            if bool(entry.get("tracked")) != tracked:
                errors.append(f"tracked flag mismatch: {workspace_path}")
            if entry.get("repository_url") and not tracked:
                errors.append(f"GitHub URL points to untracked target: {workspace_path}")
            if not (proxies / entry["proxy"]).is_file():
                errors.append(f"workspace proxy is missing: {entry['proxy']}")

    for old_root in ("内容", "规范", "手册", "部署", "资产", ".obsidian"):
        if (ROOT / old_root).exists():
            errors.append(f"legacy top-level path remains: {old_root}")

    if errors:
        print(f"FAIL: {len(errors)} issue(s)")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"PASS: {len(notes)} notes; metadata, links, workspace proxies, and checklist sources are valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
