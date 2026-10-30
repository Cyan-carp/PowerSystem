#!/usr/bin/env python3
"""Rebuild audited Vault proxy pages for files outside ``notes/``."""

from __future__ import annotations

import json
import argparse
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tools/note-governance/workspace-links.json"
PROXY_DIR = ROOT / "notes/06-代码工作区导航与链接映射"
REPOSITORY_BASE = "https://github.com/Cyan-carp/PowerSystem"
# 本机 node/python 的 TZ 可能是 America/Los_Angeles，date.today() 会比
# 北京时间晚一天，写进 updated 就成了错误的“过时”日期。统一按北京时间取日。
BEIJING = timezone(timedelta(hours=8))


def today_beijing() -> str:
    return datetime.now(BEIJING).date().isoformat()


GENERATED_DATE = today_beijing()
ADDITIONAL_TARGETS = (
    "stages/04-frontend/deploy/observe-telemetry.py",
    "stages/04-frontend/deploy/test_observe_telemetry.py",
    "artifacts/telemetry-repair-20261029-r1",
    "stages/04-frontend/deploy/frontend-release-backup.py",
    "stages/04-frontend/deploy/test_compatible_release.py",
    ".github/workflows/release-checks.yml",
    "artifacts/workflow-efficiency-20261028-r1",
    "stages/11-desktop-client/README.md",
    "README.md",
    "AGENTS.md",
    "artifacts/stage8-agent-rag/server-20261011/evidence/acceptance/browser-quality.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance-r3/acceptance.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance-r3/restart.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance/rollback-check.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/observation.json",
    "deploy/edge-server/独立云服务器部署/config",
    "deploy/edge-server/独立云服务器部署/scripts/check-backup-space.sh",
    "deploy/edge-server/独立云服务器部署/scripts/install-sftp-receiver.sh",
    "stages/04-frontend/PARITY-20261029.md",
    "stages/04-frontend/API-INVENTORY-20261029.md",
    "stages/04-frontend/DESIGN.md",
    "stages/04-frontend/vitest.config.ts",
    "stages/04-frontend/dev/mock-server.mjs",
)

# 索引分组：按序匹配，首个命中即归类。判断顺序影响结果，
# 「部署脚本」必须先于「前端实现」，「阶段 README」必须先于「部署脚本」。
GROUPS = (
    ("私有证据（本机产物，不入库）", lambda path: path.startswith("artifacts/")),
    ("阶段运行说明", lambda path: re.fullmatch(r"stages/\d+-[^/]+/README\.md", path) is not None),
    (
        "部署与发布工具",
        lambda path: path.startswith("stages/04-frontend/deploy/")
        or path.startswith("deploy/")
        or path.startswith(".github/"),
    ),
    ("前端实现与联调", lambda path: path.startswith("stages/04-frontend/")),
    ("仓库根文档", lambda path: "/" not in path),
)

GROUP_NOTES = {
    "私有证据（本机产物，不入库）": (
        "下列目标被 `.gitignore` 排除，只在本机存在；`updated` 是产物自身的落盘/提交时间，"
        "表示该次取证的完成时点，不代表当前线上状态。"
    ),
    "前端实现与联调": (
        "前端技术事实与实现规则分别维护在 01 分区「04-前端、上线与沉淀」的当前前端工程说明"
        "与 02 分区「发布仓库与界面验收规范」；本组只放入口，不复制结论。"
    ),
}


def tracked(path: str) -> bool:
    target = ROOT / path
    if not target.is_file():
        return False
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", path],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def proxy_name(path: str) -> str:
    if path == "README.md":
        return "root-README.md"
    safe = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", path.replace("/", "-")).strip("-.")
    return safe if safe.casefold().endswith(".md") else safe + ".md"


def last_changed(path: str) -> str:
    """Return the target's own last change date, never the generation date.

    Tracked paths take the last commit touching them; ignored paths (private
    evidence, local dev helpers) fall back to filesystem mtime.  Writing a
    fixed date here makes every proxy page look stale a week later.
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cs", "--", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    value = result.stdout.strip()
    if result.returncode == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return value
    target = ROOT / path
    if target.exists():
        stamp = datetime.fromtimestamp(target.stat().st_mtime, BEIJING)
        return stamp.date().isoformat()
    return GENERATED_DATE


def group_of(path: str) -> str:
    for name, matches in GROUPS:
        if matches(path):
            return name
    return "其他入口"


def github_url(path: str) -> str:
    return f"{REPOSITORY_BASE}/blob/main/{quote(path, safe='/.-_')}"


def relative_target(path: str) -> str:
    """Return a portable path from a proxy page to its workspace target."""
    return Path(os.path.relpath(ROOT / path, PROXY_DIR)).as_posix()


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "--", path],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def proxy_source(entry: dict[str, object]) -> str:
    path = str(entry["workspace_path"])
    updated = last_changed(path)
    repository_url = entry.get("repository_url")
    repository_yaml = str(repository_url) if repository_url else "null"
    is_dir = (ROOT / path).is_dir()
    ignored = is_ignored(path)
    if is_dir:
        kind = "目录，进入后按文件查看" + ("；按 `.gitignore` 不入库" if ignored else "")
        target_line = f"- 工作区目标：`{entry['relative_path']}`（{kind}）"
    elif ignored:
        # 按 .gitignore 排除的本地件：全新克隆中不存在，不能渲染成可点链接。
        target_line = f"- 工作区目标：`{entry['relative_path']}`（**按 `.gitignore` 不入库**，仅在本机存在）"
    else:
        target_line = f"- 工作区目标：[打开相对路径目标]({entry['relative_path']})"
    lines = [
        "---",
        "tags:",
        "  - 源网智联",
        "  - 工作区导航",
        "status: 现行",
        "scope: 跨版本",
        f"updated: {updated}",
        f'workspace_path: "{path}"',
        f'relative_path: "{entry["relative_path"]}"',
        f'repository_url: "{repository_yaml}"' if repository_url else "repository_url: null",
        f"tracked: {'true' if entry['tracked'] else 'false'}",
        "---",
        "",
        f"# 工作区链接 · `{path}`",
        "",
        target_line,
    ]
    if repository_url:
        repo_line = f"- 仓库目标：[在 GitHub 中查看]({repository_url})"
    elif ignored:
        repo_line = "- 仓库目标：该路径被 `.gitignore` 排除（本机产物或本地联调件），不生成 GitHub 链接。"
    elif is_dir:
        repo_line = "- 仓库目标：Git 不跟踪目录对象，GitHub 链接只给目录内已入库的单个文件。"
    else:
        repo_line = "- 仓库目标：该路径尚未入库（未 `git add`），因此不生成 GitHub 链接。"
    lines.append(repo_line)
    lines.extend(
        [
            "",
            "> 本页由 `tools/note-governance/build_workspace_links.py` 生成；链接以本代理页为基准使用仓库相对路径，不记录本机用户名或绝对目录。",
            "",
            "## 返回",
            "",
            "- [[06-代码工作区导航与链接映射/00-索引·代码工作区导航]]",
            "",
        ]
    )
    return "\n".join(lines)


def write_atomic(path: Path, text: str) -> None:
    """Write UTF-8/LF with retries.

    Windows 上杀软或索引器偶发占用会让 write_text 抛 Errno 22；重试即可，
    不能因为一次瞬时占用就中断整批代理页重建。
    """
    for attempt in range(4):
        try:
            path.write_text(text, encoding="utf-8", newline="\n")
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(0.1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=date.fromisoformat,
                        help='explicit client-local date for newly generated proxy metadata')
    args = parser.parse_args()
    global GENERATED_DATE
    if args.date:
        GENERATED_DATE = args.date.isoformat()
    old_entries = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    old_proxy = {str(item["workspace_path"]): str(item["proxy"]) for item in old_entries}
    targets = list(dict.fromkeys([str(item["workspace_path"]) for item in old_entries] + list(ADDITIONAL_TARGETS)))

    entries: list[dict[str, object]] = []
    for path in targets:
        target = ROOT / path
        if not target.exists():
            raise FileNotFoundError(path)
        is_tracked = tracked(path)
        entries.append(
            {
                "workspace_path": path,
                "proxy": old_proxy.get(path, proxy_name(path)),
                "relative_path": relative_target(path),
                "repository_url": github_url(path) if is_tracked else None,
                "tracked": is_tracked,
            }
        )

    PROXY_DIR.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        write_atomic(PROXY_DIR / str(entry["proxy"]), proxy_source(entry))

    index_lines = [
        "---",
        "tags:",
        "  - 源网智联",
        "  - 工作区导航",
        "status: 现行",
        "scope: 跨版本",
        f"updated: {GENERATED_DATE}",
        "---",
        "",
        "# 代码工作区导航与链接映射",
        "",
        "> 本目录将 Obsidian 笔记连接到 Vault 外部的代码、部署脚本、私有证据和工具。代理页由治理工具生成，"
        "每页 `updated` 取该目标自身的最后变更日，不是生成日。",
        "",
        "> 工作区结构变化后运行 `python tools/note-governance/build_workspace_links.py` 重算；"
        "`artifacts/` 与 `stages/04-frontend/dev/` 按 `.gitignore` 不入库，换机后这些入口可能为空。",
        "",
    ]
    grouped: dict[str, list[dict[str, object]]] = {}
    for entry in sorted(entries, key=lambda item: str(item["workspace_path"])):
        grouped.setdefault(group_of(str(entry["workspace_path"])), []).append(entry)
    order = [name for name, _ in GROUPS] + ["其他入口"]
    for name in order:
        items = grouped.get(name)
        if not items:
            continue
        index_lines.extend([f"## {name}", ""])
        note = GROUP_NOTES.get(name)
        if note:
            index_lines.extend([f"> {note}", ""])
        for entry in items:
            stem = Path(str(entry["proxy"])).stem
            index_lines.append(f"- [[06-代码工作区导航与链接映射/{stem}|{entry['workspace_path']}]]")
        index_lines.append("")
    write_atomic(PROXY_DIR / "00-索引·代码工作区导航.md", "\n".join(index_lines))
    write_atomic(MANIFEST, json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    print(f"generated {len(entries)} workspace proxies")


if __name__ == "__main__":
    main()
