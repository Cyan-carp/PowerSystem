"""Read the running Agent's private knowledge misses over SSH and open a local report."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT = REPO_ROOT / "artifacts/stage7-智能体/private/reports/knowledge-misses.html"
MAX_REPLY_BYTES = 16 * 1024 * 1024
CONTAINER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")

# Executed through `ssh ... python3 -`, without placing a script on the server.
REMOTE_READER = r'''
import json, pathlib, re, subprocess, sys

container = sys.argv[1]
result = subprocess.run(["docker", "inspect", "--format", "{{json .Mounts}}", container],
                        text=True, capture_output=True, check=True)
mounts = json.loads(result.stdout)
sources = [m["Source"] for m in mounts if m.get("Destination") == "/knowledge-misses" and m.get("Type") == "bind"]
if len(sources) != 1:
    raise RuntimeError("Agent must have exactly one /knowledge-misses bind mount")
directory = pathlib.Path(sources[0])
if not directory.is_dir() or directory.is_symlink():
    raise RuntimeError("knowledge-misses source is not a regular directory")
records, names, total_size = [], [], 0
for path in sorted(directory.glob("misses-????????.jsonl")):
    if not re.fullmatch(r"misses-\d{8}\.jsonl", path.name) or not path.is_file() or path.is_symlink():
        continue
    total_size += path.stat().st_size
    if total_size > 12 * 1024 * 1024:
        raise RuntimeError("knowledge-misses files exceed the read-only report limit")
    names.append(path.name)
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("normalized_question"), str) or not isinstance(row.get("at"), str):
                    raise ValueError("required fields missing")
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("invalid JSONL at %s:%d" % (path.name, number)) from exc
            records.append(row)
            if len(records) > 20000:
                raise RuntimeError("knowledge-misses record count exceeds the report limit")
status_path = directory / "review-status.json"
statuses = {}
if status_path.exists():
    if not status_path.is_file() or status_path.is_symlink() or status_path.stat().st_size > 1024 * 1024:
        raise RuntimeError("review-status.json is not a regular small file")
    statuses = json.loads(status_path.read_text(encoding="utf-8"))
    if not isinstance(statuses, dict):
        raise RuntimeError("review-status.json must be an object")
print(json.dumps({"records": records, "statuses": statuses, "files": names,
                  "source": str(directory)}, ensure_ascii=False))
'''


def ssh_program() -> str:
    windows_ssh = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
    if os.name == "nt" and windows_ssh.is_file():
        return str(windows_ssh)
    found = shutil.which("ssh")
    if not found:
        raise RuntimeError("找不到 OpenSSH 客户端 ssh")
    return found


def fetch(host: str, port: int, user: str, identity: Path, container: str) -> dict:
    if not identity.is_file():
        raise RuntimeError("私钥文件不存在")
    if not CONTAINER_PATTERN.fullmatch(container):
        raise RuntimeError("容器名称不合法")
    command = [ssh_program(), "-i", str(identity), "-p", str(port),
               "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ConnectTimeout=10", "-o", "NumberOfPasswordPrompts=0",
               "-T", f"{user}@{host}", f"python3 - {container}"]
    try:
        result = subprocess.run(command, input=REMOTE_READER, text=True,
                                capture_output=True, timeout=45, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("SSH 读取超时；没有生成报告") from exc
    if result.returncode:
        detail = (result.stderr or "SSH 连接或远端读取失败").strip().splitlines()[-1]
        raise RuntimeError(f"远端读取失败：{detail}")
    if len(result.stdout.encode("utf-8")) > MAX_REPLY_BYTES:
        raise RuntimeError("远端记录超过报告大小限制")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("远端输出不是完整 JSON；没有生成报告") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list) or not isinstance(payload.get("statuses"), dict):
        raise RuntimeError("远端记录结构不符合预期")
    return payload


def summarize(payload: dict) -> list[dict]:
    groups: dict[str, dict] = {}
    for row in payload["records"]:
        if not isinstance(row, dict) or not isinstance(row.get("normalized_question"), str) or not isinstance(row.get("at"), str):
            raise RuntimeError("记录缺少问题或时间字段")
        question = row["normalized_question"]
        identifier = hashlib.sha256(question.encode("utf-8")).hexdigest()[:20]
        group = groups.setdefault(identifier, {"id": identifier, "question": question,
                                               "count": 0, "last_seen": "", "reasons": set(),
                                               "web_statuses": set(), "records": []})
        group["count"] += 1
        group["last_seen"] = max(group["last_seen"], row["at"])
        group["reasons"].add(str(row.get("reason", "未知")))
        group["web_statuses"].add(str(row.get("web_status", "未知")))
        group["records"].append(row)
    statuses = payload["statuses"]
    for group in groups.values():
        review = statuses.get(group["id"], {"status": "pending"})
        group["review"] = review if isinstance(review, dict) else {"status": "unknown"}
        group["records"].sort(key=lambda row: row["at"], reverse=True)
    return sorted(groups.values(), key=lambda group: (-group["count"], group["id"]))


def render(payload: dict, generated_at: str) -> str:
    groups = summarize(payload)
    escape = lambda value: html.escape(str(value), quote=True)
    cards = []
    for group in groups:
        status = str(group["review"].get("status", "unknown"))
        if status not in {"pending", "documented", "dismissed"}:
            status = "unknown"
        raw_items = "".join(
            "<details class='record'><summary>" + escape(row["at"]) + " · "
            + escape(row.get("web_status", "未知")) + " · 请求 "
            + escape(row.get("request_id", "—")) + "</summary><pre>"
            + escape(json.dumps(row, ensure_ascii=False, indent=2)) + "</pre></details>"
            for row in group["records"]
        )
        cards.append(
            "<article class='card' data-status='" + status + "'>"
            "<div class='meta'><span class='badge'>" + escape(status) + "</span>"
            "<span>出现 " + str(group["count"]) + " 次</span><span>最近 " + escape(group["last_seen"]) + "</span></div>"
            "<h2>" + escape(group["question"]) + "</h2>"
            "<p>原因：" + escape("、".join(sorted(group["reasons"])))
            + "　联网：" + escape("、".join(sorted(group["web_statuses"]))) + "</p>"
            "<details><summary>查看逐条记录（" + str(group["count"]) + "）</summary>" + raw_items + "</details></article>"
        )
    content = "".join(cards) or "<p class='empty'>当前目录没有知识缺口记录。</p>"
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'none'; connect-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>远程知识缺口记录</title>
<style>body{font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;background:#f5f6f7;color:#17232d;margin:0}main{max-width:1100px;margin:auto;padding:32px 20px}h1{font-size:28px;margin:0 0 8px}.sub{color:#56636e;margin:0 0 22px}.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}input,select{font:inherit;padding:9px 12px;border:1px solid #bac5cc;border-radius:6px;background:white}input{flex:1;min-width:240px}.card{background:white;border:1px solid #dce2e6;border-radius:8px;padding:16px 20px;margin:12px 0}.card h2{font-size:17px;overflow-wrap:anywhere;margin:10px 0}.card p{color:#43525d}.meta{display:flex;gap:15px;flex-wrap:wrap;color:#56636e;font-size:13px}.badge{background:#e8eef2;color:#243b4a;border-radius:4px;padding:1px 8px}summary{cursor:pointer}details.record{border-top:1px solid #e5e9ec;padding:8px 0;margin:8px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f7;padding:12px;border-radius:5px;font-size:12px}.empty{padding:30px;background:white;border-radius:8px}</style></head><body><main>
<h1>远程知识缺口记录</h1><p class="sub">读取时间：""" + escape(generated_at) + "　记录 " + str(len(payload["records"])) + " 条 / 去重 " + str(len(groups)) + " 类<br>来源：" + escape(payload.get("source", "未知")) + "；服务器只读，本地报告下次运行覆盖。报告可能含未识别的敏感自由文本，请勿分享。</p>" + """
<div class="toolbar"><input id="search" type="search" placeholder="搜索问题、原因或记录内容"><select id="status"><option value="">全部审核状态</option><option value="pending">待处理</option><option value="documented">已入库</option><option value="dismissed">已忽略</option><option value="unknown">未知</option></select></div>
<p id="visible"></p><section id="cards">""" + content + """</section></main><script>
const search=document.getElementById('search'),status=document.getElementById('status'),cards=[...document.querySelectorAll('.card')],visible=document.getElementById('visible');
function filter(){const q=search.value.trim().toLocaleLowerCase(),s=status.value;let n=0;for(const card of cards){const show=(!s||card.dataset.status===s)&&(!q||card.textContent.toLocaleLowerCase().includes(q));card.hidden=!show;if(show)n++}visible.textContent='显示 '+n+' / '+cards.length+' 类'}search.addEventListener('input',filter);status.addEventListener('change',filter);filter();
</script></body></html>"""


def write_report(content: str) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".html",
                                     dir=REPORT.parent, delete=False) as temporary:
        temporary.write(content)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(REPORT)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 SSH 只读查看运行中 Agent 的知识缺口")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--user", required=True)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--container", default="powersystem-stage4-agent-1")
    parser.add_argument("--no-open", action="store_true", help="只生成报告，不启动浏览器")
    args = parser.parse_args(argv)
    try:
        REPORT.unlink(missing_ok=True)  # Never leave an old report looking like this run succeeded.
        payload = fetch(args.host, args.port, args.user, args.identity, args.container)
        content = render(payload, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        write_report(content)
    except (RuntimeError, OSError, UnicodeError) as exc:
        print(f"查看失败：{exc}", file=sys.stderr)
        return 1
    print(f"已读取 {len(payload['records'])} 条，去重 {len(summarize(payload))} 类。报告：{REPORT}")
    if not args.no_open:
        webbrowser.open(REPORT.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
