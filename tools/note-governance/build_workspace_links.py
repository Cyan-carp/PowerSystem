#!/usr/bin/env python3
"""Rebuild audited Vault proxy pages for files outside ``notes/``."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tools/note-governance/workspace-links.json"
PROXY_DIR = ROOT / "notes/06-代码工作区导航与链接映射"
REPOSITORY_BASE = "https://github.com/Cyan-carp/PowerSystem"
ADDITIONAL_TARGETS = (
    "README.md",
    "artifacts/stage8-agent-rag/server-20261011/evidence/acceptance/browser-quality.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance-r3/acceptance.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance-r3/restart.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/acceptance/rollback-check.json",
    "artifacts/stage8-agent-rag/boundary/bnd-m1-20261011/server-evidence/observation.json",
    "deploy/edge-server/独立云服务器部署/config",
    "deploy/edge-server/独立云服务器部署/scripts/check-backup-space.sh",
    "deploy/edge-server/独立云服务器部署/scripts/install-sftp-receiver.sh",
)


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
    safe = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "-", path.replace("/", "-")).strip("-")
    return safe + ".md"


def github_url(path: str) -> str:
    return f"{REPOSITORY_BASE}/blob/main/{quote(path, safe='/.-_')}"


def relative_target(path: str) -> str:
    """Return a portable path from a proxy page to its workspace target."""
    return Path(os.path.relpath(ROOT / path, PROXY_DIR)).as_posix()


def proxy_source(entry: dict[str, object]) -> str:
    path = str(entry["workspace_path"])
    repository_url = entry.get("repository_url")
    repository_yaml = str(repository_url) if repository_url else "null"
    lines = [
        "---",
        "tags:",
        "  - 源网智联",
        "  - 工作区导航",
        "status: 现行",
        "scope: 跨版本",
        "updated: 2026-10-17",
        f'workspace_path: "{path}"',
        f'relative_path: "{entry["relative_path"]}"',
        f'repository_url: "{repository_yaml}"' if repository_url else "repository_url: null",
        f"tracked: {'true' if entry['tracked'] else 'false'}",
        "---",
        "",
        f"# 工作区链接 · `{path}`",
        "",
        f"- 工作区目标：[打开相对路径目标]({entry['relative_path']})",
    ]
    if repository_url:
        lines.append(f"- 仓库目标：[在 GitHub 中查看]({repository_url})")
    else:
        lines.append("- 仓库目标：当前路径尚未被 Git 以该路径跟踪，因此不生成 GitHub 链接。")
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


def main() -> None:
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
        (PROXY_DIR / str(entry["proxy"])).write_text(proxy_source(entry), encoding="utf-8", newline="\n")

    index_lines = [
        "---",
        "tags:",
        "  - 源网智联",
        "  - 工作区导航",
        "status: 现行",
        "scope: 跨版本",
        "updated: 2026-10-17",
        "---",
        "",
        "# 代码工作区导航与链接映射",
        "",
        "> 本目录将 Obsidian 笔记连接到 Vault 外部的代码、部署脚本、私有证据和工具。代理页由治理工具生成。",
        "",
        "## 工作区入口",
        "",
    ]
    for entry in sorted(entries, key=lambda item: str(item["workspace_path"])):
        note = Path(str(entry["proxy"])).stem
        index_lines.append(f"- [[06-代码工作区导航与链接映射/{note}|{entry['workspace_path']}]]")
    index_lines.append("")
    (PROXY_DIR / "00-索引·代码工作区导航.md").write_text("\n".join(index_lines), encoding="utf-8", newline="\n")
    MANIFEST.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"generated {len(entries)} workspace proxies")


if __name__ == "__main__":
    main()
