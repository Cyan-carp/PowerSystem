#!/usr/bin/env python3
"""Validate the isolated Obsidian vault and the project Markdown that links into it.

Usage:
    python tools/note-governance/check_notes.py            # 本地严格模式
    python tools/note-governance/check_notes.py --ci       # CI 模式
    python tools/note-governance/check_notes.py --verbose  # 列出全部警告

两种模式只在一处不同：`artifacts/` 下的私有证据按设计不入库，全新克隆里必然
缺失，所以 CI 模式不把“解析后落在仓库根 artifacts/ 内”的缺失链接判为错误。
判定按解析后的路径边界，不看字符串里是否出现 `artifacts`。

检查范围分三层：

1. `notes/` Vault：元数据、重名、目录索引、双链、Markdown 链接、章节锚点、
   可达性、迁移残留、验收清单来源。
2. 库外项目 Markdown：根 `README.md`/`AGENTS.md`/`SKILL.md`、`stages/`、
   `deploy/`、`tools/`、`shared-memory/` 三个入口文件中的链接。
3. `shared-memory/conversations/` 会话历史：只做统计，不回判历史旧路径。

退出码 1 表示存在错误；警告不改变退出码。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import note_links as nl  # noqa: E402  (同目录模块，需先补 sys.path)

ROOT = Path(__file__).resolve().parents[2]
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

# Vendored dependencies and generated trees carry their own READMEs; they are not
# project documentation and must not be judged by this repository's link rules.
SKIP_PARTS = {
    "node_modules",
    ".git",
    "artifacts",
    "dist",
    "build",
    "vendor",
    "__pycache__",
    ".venv",
    "site",
}
WORKSPACE_ENTRIES = ("README.md", "AGENTS.md", "SKILL.md")
WORKSPACE_TREES = ("stages", "deploy", "tools")
SHARED_ENTRIES = ("README.md", "workbuddy-collaboration.md", "project-memory.md")
ENTRY_NOTES = ("00-项目笔记总入口.md", "00-当前系统状态.md")

# 例外必须精确到对象并写明理由；不要用目录级忽略把结果压成零。
LINK_EXCEPTIONS: dict[tuple[str, int, str], str] = {}

# 交接目录的 00-入口只列“本轮相关”的交接页，完整列表由版本索引承担；
# 这里按目录豁免“每页必须被直接列出”，不是全库放宽。
INDEX_COVERAGE_SKIP = {
    "notes/01-项目调研规划与开发建设记录/04-第四版-智能助手行动化重构/03-跨对话交接": "接续入口只列本轮相关交接，完整列表见第四版索引",
    "notes/01-项目调研规划与开发建设记录/05-第五版-Windows桌面客户端/03-跨对话交接": "同上，接续入口按轮次收窄",
    "notes/01-项目调研规划与开发建设记录/06-第六版-预测质量闭环/03-跨对话交接": "同上",
    "notes/01-项目调研规划与开发建设记录/07-第七版-模型换版闭环/03-跨对话交接": "同上",
    "notes/01-项目调研规划与开发建设记录/08-第八版-移动端与安卓客户端/03-跨对话交接": "同上",
}


@dataclass(frozen=True)
class Issue:
    severity: str  # error | warning | info
    kind: str
    path: str
    line: int
    target: str
    message: str

    def render(self) -> str:
        location = f"{self.path}:{self.line}" if self.line else self.path
        target = f" -> {self.target}" if self.target else ""
        return f"[{self.kind}] {location}{target} {self.message}"


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


def git_tracked_files(root: Path = ROOT) -> set[str]:
    """One `git ls-files` call; per-entry subprocesses made the check needlessly slow."""

    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {line.replace("\\", "/") for line in result.stdout.decode("utf-8", "replace").splitlines()}


def load_migration(path: Path) -> list[tuple[str, str]]:
    """(old, new) pairs from the migration map, used to explain stale paths."""

    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for item in data.get("moves", []):
        pairs.append((str(item.get("from", "")), str(item.get("to", ""))))
    for old, new in (data.get("files") or {}).items():
        pairs.append((str(old), str(new)))
    return [(old, new) for old, new in pairs if old and new]


def rewrite(target: str, pairs: list[tuple[str, str]]) -> str:
    """Follow the migration map until the path stops changing (bounded)."""

    value = target
    for _ in range(6):
        changed = False
        for old, new in pairs:
            if value == old or value.startswith(old.rstrip("/") + "/"):
                replaced = new + value[len(old) :] if value != old else new
                if replaced != value:
                    value, changed = replaced, True
        if not changed:
            break
    return value


def migrated_location(target: str, pairs: list[tuple[str, str]], root: Path) -> str | None:
    """Migrated path that still exists, accepting the implicit `.md` suffix.

    Links are written vault-relative (`[[04-.../69-日志]]`) while the migration map
    records repository paths (`notes/04-.../69-日志.md`), so both forms are tried.
    """

    for candidate in (target, f"{target}.md", f"notes/{target}", f"notes/{target}.md"):
        moved = rewrite(candidate, pairs)
        if moved == candidate:
            continue
        path = root / moved
        for option in (path, Path(str(path) + ".md"), path.with_suffix(".md")):
            if option.is_file():
                return option.relative_to(root).as_posix()
    return None


def walk_markdown(base: Path) -> list[Path]:
    """Markdown under `base` with vendor/build trees pruned before descending.

    `Path.rglob("*.md")` walks `node_modules` and Go module caches in full, which
    dominated the runtime for no benefit.
    """

    found: list[Path] = []
    for current, dirs, files in os.walk(base):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_PARTS)
        found.extend(Path(current) / name for name in sorted(files) if name.endswith(".md"))
    return found


def index_of(directory: Path, notes_by_dir: dict[Path, list[Path]] | None = None) -> Path | None:
    if notes_by_dir is None:
        for candidate in sorted(directory.glob("00-*.md")):
            return candidate
        return None
    for candidate in sorted(notes_by_dir.get(directory, [])):
        if candidate.name.startswith("00-"):
            return candidate
    return None


def run(root: Path = ROOT, ci_mode: bool = False, verbose: bool = False) -> tuple[list[Issue], Counter]:
    vault = root / "notes"
    manifest = root / "tools/note-governance/workspace-links.json"
    migration = root / "tools/note-governance/migration-map.json"
    checklist = vault / "02-工程规范边界与验收治理/项目验收与收尾清单"
    shared = root / "shared-memory"
    issues: list[Issue] = []
    stats: Counter = Counter()

    def rel(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    def add(severity: str, kind: str, path: Path | str, line: int, target: str, message: str) -> None:
        issues.append(Issue(severity, kind, path if isinstance(path, str) else rel(path), line, target, message))

    def private_evidence(path: Path) -> bool:
        try:
            path.resolve().relative_to((root / "artifacts").resolve())
        except (ValueError, OSError):
            return False
        return True

    def skipped(path: Path) -> bool:
        try:
            parts = path.relative_to(root).parts
        except ValueError:
            return True
        return any(part in SKIP_PARTS for part in parts[:-1])

    nl.reset_cache()
    notes = sorted(vault.rglob("*.md"))
    vault_dirs = sorted(path for path in vault.rglob("*") if path.is_dir())
    by_stem: dict[str, list[Path]] = defaultdict(list)
    notes_by_dir: dict[Path, list[Path]] = defaultdict(list)
    page_cache: dict[Path, str] = {}

    def page_text(path: Path) -> str:
        """One read per page: anchors can be checked from several sources."""

        value = page_cache.get(path)
        if value is None:
            value = path.read_text(encoding="utf-8-sig")
            page_cache[path] = value
        return value

    for path in vault.rglob("*"):
        if path.is_file():
            by_stem[path.stem.casefold()].append(path)
    for path in notes:
        notes_by_dir[path.parent].append(path)
    stats["notes"] = len(notes)

    # ---------- 1. 元数据、体量、重名 ----------
    duplicate_names = [name for name, count in Counter(p.name.casefold() for p in notes).items() if count > 1]
    if duplicate_names:
        add("error", "duplicate-basename", "notes", 0, ", ".join(sorted(duplicate_names)), "同名 Markdown 文件")
    for path in notes:
        text = path.read_text(encoding="utf-8-sig")
        meta = frontmatter(text)
        if meta.get("status") not in VALID_STATUS:
            add("error", "metadata", path, 0, "", f"status 缺失或非法：{meta.get('status')!r}")
        if meta.get("scope") not in VALID_SCOPE:
            add("error", "metadata", path, 0, "", f"scope 缺失或非法：{meta.get('scope')!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta.get("updated", "")):
            add("error", "metadata", path, 0, "", f"updated 缺失或非 ISO 日期：{meta.get('updated')!r}")
        if len(text) > 15_000:
            add("error", "oversized", path, 0, "", f"{len(text)} 字符，超过 15000 上限")

    # ---------- 2. 目录索引与旧目录 ----------
    for directory in vault_dirs:
        direct = notes_by_dir.get(directory, [])
        if len(direct) <= 1 or index_of(directory, notes_by_dir) is not None:
            continue
        if nl.cached_is_file(directory.parent / f"{directory.name}.md"):
            continue
        add("error", "missing-index", directory, 0, "", "多篇笔记目录缺少 00- 索引")
    for base in (vault, root / "stages", root / "deploy", root / "tools"):
        for directory in base.rglob("*"):
            if directory.is_dir() and directory.name.startswith("开发阶段"):
                add("error", "legacy-dir", directory, 0, "", "遗留“开发阶段”目录")

    # ---------- 3. Vault 链接、章节与迁移残留 ----------
    pairs = load_migration(migration)
    edges: dict[Path, set[Path]] = defaultdict(set)
    for path in notes:
        text = page_text(path)
        for link in nl.iter_links(text):
            stats[f"link-{link.kind}"] += 1
            if link.kind == "wikilink" and link.target.startswith(("stages/", "deploy/", "artifacts/", "tools/")):
                add("error", "workspace-wikilink", path, link.line, link.raw, "Vault 内不得用双链指向工作区路径")
                continue
            if not link.target:
                if link.subpath:
                    result = nl.anchor_status(text, link.subpath, nl.dialect_of(link.kind))
                    if result == "missing":
                        add("error", "broken-anchor", path, link.line, link.raw, "同页锚点不存在")
                    elif result == "ambiguous":
                        add("warning", "ambiguous-anchor", path, link.line, link.raw, "同页重复标题，锚点有歧义")
                continue
            if (rel(path), link.line, link.raw) in LINK_EXCEPTIONS:
                continue
            resolved = nl.resolve(link.target, path, by_stem, vault=vault, kind=link.kind, root=root)
            if resolved.status == "external":
                continue
            if resolved.status == "case":
                add("error", "case-mismatch", path, link.line, link.raw, f"大小写不符，Linux/GitHub 打不开：应为 {resolved.detail}")
                continue
            if resolved.status == "ambiguous":
                add("error", "ambiguous-link", path, link.line, link.raw, resolved.detail)
                continue
            if resolved.status == "outside":
                add("error", "outside-link", path, link.line, link.raw, "越出 Vault 或仓库的相对路径")
                continue
            if resolved.status != "ok" or resolved.path is None:
                suggestion = migrated_location(link.target, pairs, root)
                if suggestion:
                    add("error", "migration-residue", path, link.line, link.raw, f"仍指向迁移前路径，现行位置：{suggestion}")
                elif ci_mode and private_evidence(resolved.path or root / link.target):
                    stats["skipped-private"] += 1
                else:
                    add("error", "broken-link", path, link.line, link.raw, "目标不存在")
                continue
            target_path = resolved.path
            # 锚点与目录边界无关：指向库外 README 的 `…#章节` 同样会失效，
            # 只有目标是否参与可达性图才需要区分 Vault 内外。
            if target_path.suffix.casefold() in {".md", ".markdown"}:
                if target_path.is_relative_to(vault):
                    edges[path].add(target_path)
                if link.subpath:
                    result = nl.anchor_status(page_text(target_path), link.subpath, nl.dialect_of(link.kind))
                    if result == "missing":
                        add("error", "broken-anchor", path, link.line, link.raw, "目标页没有该章节或块")
                    elif result == "ambiguous":
                        add("warning", "ambiguous-anchor", path, link.line, link.raw, "目标页重复标题，锚点有歧义")

    # ---------- 4. 索引覆盖与全库可达 ----------
    for directory in vault_dirs:
        index = index_of(directory, notes_by_dir)
        if index is None:
            continue
        # 同目录可能有多个索引（总索引 + 按月索引）；被任一索引列出即算已登记，
        # 不要求每个索引重复列全部后代。
        indexes = [
            child
            for child in notes_by_dir.get(directory, [])
            if child.name.startswith("00-") or "索引" in child.name
        ]
        listed = set().union(*(edges.get(item, set()) for item in indexes)) if indexes else set()
        children = [child for child in notes_by_dir.get(directory, []) if child not in indexes]
        if rel(directory) not in INDEX_COVERAGE_SKIP:
            for child in children:
                if child not in listed and child.name not in ENTRY_NOTES:
                    add("warning", "index-not-listed", child, 0, index.name, "未被本目录任一索引直接列出")
        inbound = [source for source, targets in edges.items() if index in targets and source != index]
        if not inbound:
            add("warning", "index-unlinked", index, 0, "", "索引没有任何入链，无法从上级进入")

    reached: set[Path] = set()
    queue: deque[Path] = deque(path for path in (vault / name for name in ENTRY_NOTES) if path.is_file())
    while queue:
        current = queue.popleft()
        if current in reached:
            continue
        reached.add(current)
        queue.extend(sorted(edges.get(current, ())))
    unreached = [path for path in notes if path not in reached]
    for path in unreached:
        add("warning", "unreachable", path, 0, "", "从总入口不可达")
    stats["unreachable"] = len(unreached)

    # ---------- 5. 库外项目 Markdown ----------
    workspace_files: list[Path] = [root / name for name in WORKSPACE_ENTRIES]
    for tree in WORKSPACE_TREES:
        workspace_files.extend(walk_markdown(root / tree))
    workspace_files.extend(shared / name for name in SHARED_ENTRIES)
    workspace_files = [path for path in workspace_files if path.is_file() and not skipped(path)]
    stats["workspace-files"] = len(workspace_files)
    for path in workspace_files:
        text = page_text(path)
        for link in nl.iter_links(text):
            if not link.target:
                if link.subpath:
                    result = nl.anchor_status(text, link.subpath, nl.dialect_of(link.kind))
                    if result == "missing":
                        add("error", "broken-anchor", path, link.line, link.raw, "同页锚点不存在")
                    elif result == "ambiguous":
                        add("warning", "ambiguous-anchor", path, link.line, link.raw, "同页重复标题，锚点有歧义")
                continue
            if link.kind == "wikilink":
                target = link.target
                if target.startswith("notes/"):
                    target = target[len("notes/") :]
                elif target.split("/")[0] in WORKSPACE_TREES:
                    add("warning", "workspace-wikilink", path, link.line, link.raw, "库外 Markdown 使用双链，GitHub 不解析")
                    continue
                resolved = nl.resolve(target, path, by_stem, vault=vault, kind="wikilink", notes_only=True, root=root)
                if resolved.status == "ok" and resolved.path is not None:
                    stats["workspace-wikilink-ok"] += 1
                    if link.subpath:
                        result = nl.anchor_status(page_text(resolved.path), link.subpath, "obsidian")
                        if result == "missing":
                            add("error", "broken-anchor", path, link.line, link.raw, "目标页没有该章节或块")
                        elif result == "ambiguous":
                            add("warning", "ambiguous-anchor", path, link.line, link.raw, "目标页重复标题，锚点有歧义")
                else:
                    add("error", "broken-link", path, link.line, link.raw, f"笔记不存在（{resolved.status}）")
                continue
            if (rel(path), link.line, link.raw) in LINK_EXCEPTIONS:
                continue
            resolved = nl.resolve(link.target, path, by_stem, kind="markdown", root=root)
            if resolved.status in ("external", "empty"):
                continue
            if resolved.status == "case":
                add("error", "case-mismatch", path, link.line, link.raw, f"大小写不符，Linux/GitHub 打不开：应为 {resolved.detail}")
                continue
            if resolved.status != "ok":
                if ci_mode and private_evidence(path.parent / nl.normalise(link.target)):
                    stats["skipped-private"] += 1
                else:
                    add("error", "broken-link", path, link.line, link.raw, f"目标不存在（{resolved.status}）")
                continue
            if resolved.path is not None and link.subpath and resolved.path.suffix.casefold() in {".md", ".markdown"}:
                result = nl.anchor_status(page_text(resolved.path), link.subpath, "markdown")
                if result == "missing":
                    add("error", "broken-anchor", path, link.line, link.raw, "目标页没有该章节（GitHub 锚点）")
                elif result == "ambiguous":
                    add("warning", "ambiguous-anchor", path, link.line, link.raw, "目标页重复标题，锚点有歧义")

    # ---------- 6. 会话历史：只统计，不回判 ----------
    history_dir = shared / "conversations"
    if history_dir.is_dir():
        broken = 0
        total = 0
        for path in walk_markdown(history_dir):
            if skipped(path):
                continue
            for link in nl.iter_links(path.read_text(encoding="utf-8-sig")):
                if not link.target or link.kind == "wikilink":
                    continue
                total += 1
                resolved = nl.resolve(link.target, path, by_stem, kind="markdown", root=root)
                if resolved.status not in ("ok", "external", "empty"):
                    broken += 1
        stats["history-links"] = total
        stats["history-unresolved"] = broken

    # ---------- 7. 验收清单来源 ----------
    if not checklist.is_dir():
        add("error", "checklist", checklist, 0, "", "验收清单目录缺失")
    else:
        for path in sorted(checklist.glob("*.md")):
            for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
                if re.match(r"^\s*- \[ \]", line) and "来源：" not in line:
                    add("error", "checklist-source", path, number, "", "未勾选条目缺少“来源：”")

    # ---------- 8. 工作区代理清单 ----------
    if not manifest.is_file():
        add("error", "manifest", manifest, 0, "", "工作区链接清单缺失")
    else:
        proxies = vault / "05-代码工作区导航与链接映射"
        tracked_files = git_tracked_files(root)
        for entry in json.loads(manifest.read_text(encoding="utf-8")):
            workspace_path = entry["workspace_path"]
            target = root / workspace_path
            proxy_file = proxies / entry["proxy"]
            if not target.exists():
                if entry.get("tracked") or not ci_mode:
                    add("error", "manifest", manifest, 0, workspace_path, "工作区目标不存在")
                elif not proxy_file.is_file():
                    add("error", "manifest", proxy_file, 0, entry["proxy"], "代理页缺失")
                continue
            expected = Path(os.path.relpath(target, proxy_file.parent)).as_posix()
            if entry.get("relative_path") != expected:
                add("error", "manifest", manifest, 0, workspace_path, f"relative_path 过期：应为 {expected}")
            elif (proxy_file.parent / str(entry.get("relative_path"))).resolve() != target.resolve():
                add("error", "manifest", proxy_file, 0, workspace_path, "代理页相对路径解析不正确")
            if not proxy_file.is_file():
                add("error", "manifest", proxy_file, 0, entry["proxy"], "代理页缺失")
            tracked = target.is_file() and workspace_path.replace("\\", "/") in tracked_files
            if bool(entry.get("tracked")) != tracked:
                add("error", "manifest", manifest, 0, workspace_path, "tracked 标记与 Git 实际不一致")
            if entry.get("repository_url") and not tracked:
                add("error", "manifest", manifest, 0, workspace_path, "GitHub URL 指向未跟踪目标")

    # ---------- 9. 遗留顶层目录 ----------
    for old_root in ("内容", "规范", "手册", "部署", "资产", ".obsidian"):
        if (root / old_root).exists():
            add("error", "legacy-path", root / old_root, 0, "", "遗留顶层目录")

    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    if not verbose:
        issues = errors + warnings[:40]
    return issues, stats


def main() -> int:
    issues, stats = run(ci_mode="--ci" in sys.argv[1:], verbose="--verbose" in sys.argv[1:])
    errors = [issue for issue in issues if issue.severity == "error"]
    warnings = [issue for issue in issues if issue.severity == "warning"]
    for issue in issues:
        print(issue.render())
    if errors:
        print(f"FAIL: {len(errors)} error(s), {len(warnings)} warning(s)")
        print("错误类型：" + ", ".join(f"{name}={count}" for name, count in sorted(Counter(i.kind for i in errors).items())))
        return 1
    print(
        "PASS: "
        f"{stats['notes']} notes, {stats['workspace-files']} workspace markdown; "
        f"links {stats['link-wikilink'] + stats['link-markdown'] + stats['link-reference']}, "
        f"unreachable {stats['unreachable']}, warnings {len(warnings)}, "
        f"private-skipped {stats['skipped-private']}, "
        f"history-links {stats['history-links']} (unresolved {stats['history-unresolved']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
