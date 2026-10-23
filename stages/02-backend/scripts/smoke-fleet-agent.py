#!/usr/bin/env python3
"""阶段 8/9 智能体工具最小调用验证（离线）。

只读取 Codex 记录的 Go HTTP 响应 JSON，通过 httpx.MockTransport 回放：
  1. 阶段 8：rag_agent.tools.Tools.execute 真实调用链（schema -> business_route -> HTTP -> 信封 -> trace/sources）。
  2. 阶段 9：ops_agent.executor.Executor.call 真实调用链（MANIFEST -> schema -> 身份校验 -> business_route -> HTTP -> 信封）。
     platform 模块本身没有执行客户端；Executor 才是实际调用方，store 用内存桩，身份端点由 MockTransport 返回自签令牌。

不连接真实后端、数据库、Redis 或模型，不写任何文件。

用法：
    python stages/02-backend/scripts/smoke-fleet-agent.py
    python stages/02-backend/scripts/smoke-fleet-agent.py --responses artifacts/stage2/fleet-minimal-20261020-01/api-responses.json

退出码：0 全部通过；1 存在失败检查；2 输入错误；3 响应文件不存在。
"""
import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[3]
STAGE8 = REPO_ROOT / "stages" / "08-agent-rag"
STAGE9 = REPO_ROOT / "stages" / "09-ops-agent"
for _path in (STAGE8, STAGE9):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import httpx  # noqa: E402
from rag_agent.config import Config as RagConfig  # noqa: E402
from rag_agent.tools import Tools  # noqa: E402
from ops_agent import security as ops_security  # noqa: E402
from ops_agent.executor import Executor  # noqa: E402

DEFAULT_RESPONSES = REPO_ROOT / "artifacts" / "stage2" / "fleet-minimal-20261020-01" / "api-responses.json"
JWT = "smoke-jwt-offline"
ACTOR_KEY = "smoke-actor-key-0123456789abcdef0123456789abcdef"
STATION_CODE = re.compile(r"^[A-Z]{2}-\d{2}$")
PATH_ALIASES = {"dashboard/overview": ("overview", "dashboard", "overview/dashboard")}

CASES = (
    {"stage": 8, "tool": "list_stations", "arguments": {"scope": "business"}},
    {"stage": 8, "tool": "get_dashboard_overview", "arguments": {"scope": "baseline"}},
    {"stage": 9, "tool": "list_stations", "arguments": {"scope": "business"}},
    {"stage": 9, "tool": "get_dashboard_overview", "arguments": {"scope": "baseline"}},
)


def norm_path(raw):
    if "://" in raw:
        raw = urlsplit(raw).path
    raw = raw.split("?", 1)[0].strip("/")
    return raw[len("api/v1/"):] if raw.startswith("api/v1/") else raw


class Recorded:
    """把 {路径?查询: {code,message,data}} 索引成可按请求匹配的响应表。"""

    def __init__(self, mapping):
        self.entries = []
        for key, payload in mapping.items():
            if not isinstance(payload, dict) or "code" not in payload:
                raise ValueError(f"记录项 {key!r} 不是 code/message/data 结构")
            path, _, query = key.partition("?")
            self.entries.append({
                "key": key,
                "path": norm_path(path),
                "query": dict(parse_qsl(query, keep_blank_values=True)),
                "payload": payload,
            })
        if not self.entries:
            raise ValueError("响应文件没有任何记录项")

    def find(self, path, params):
        request_path = norm_path(path)
        request_query = {str(k): str(v) for k, v in params.items()}
        buckets = ([], [], [])
        for entry in self.entries:
            if entry["path"] == request_path:
                bucket = buckets[0]
            elif entry["path"] in PATH_ALIASES.get(request_path, ()):
                bucket = buckets[1]
            elif entry["path"].endswith("/" + request_path) or request_path.endswith("/" + entry["path"]):
                bucket = buckets[2]
            else:
                continue
            if all(request_query.get(k) == v for k, v in entry["query"].items()):
                bucket.append(entry)
        for level, bucket in enumerate(buckets):
            if not bucket:
                continue
            best = min(bucket, key=lambda item: len(item["query"]))
            tied = [item for item in bucket if len(item["query"]) == len(best["query"])]
            payloads = {json.dumps(item["payload"], sort_keys=True, ensure_ascii=False) for item in tied}
            if len(payloads) > 1:
                raise LookupError(f"记录中存在多个冲突响应：{request_path} {request_query}")
            return best, ("精确" if level == 0 else "别名" if level == 1 else "后缀")
        keys = "、".join(sorted(entry["key"] for entry in self.entries))
        raise LookupError(f"未找到 {request_path} {request_query} 的记录响应；现有键：{keys}")

    def by_path_scope(self, path, scope):
        try:
            entry, _ = self.find(path, {"scope": scope})
        except LookupError:
            return None
        return entry["payload"]


def walk_strings(node, predicate, found):
    if isinstance(node, dict):
        for value in node.values():
            walk_strings(value, predicate, found)
    elif isinstance(node, list):
        for value in node:
            walk_strings(value, predicate, found)
    elif isinstance(node, str) and predicate(node):
        found.append(node)


def scope_values(node):
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "scope" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(scope_values(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(scope_values(value))
    return found


def station_codes(node):
    found = []
    walk_strings(node, lambda text: bool(STATION_CODE.match(text)), found)
    return sorted(set(found))


def summarize(data):
    if not isinstance(data, dict):
        return {"type": type(data).__name__}
    info = {"keys": sorted(data)}
    for key in ("scope", "total", "partial", "computed_at",
                "expected_device_count", "registered_device_count", "observed_device_count"):
        if key in data:
            info[key] = data[key]
    for key, value in data.items():
        if isinstance(value, list):
            info[key + "_rows"] = len(value)
    summary = data.get("summary")
    if isinstance(summary, dict):
        info["summary"] = {k: summary[k] for k in ("scope", "expected", "registered", "observed", "partial", "total") if k in summary}
    return info


def add(checks, name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})


def request_checks(requests, path, params, checks):
    backend = [item for item in requests if item["path"].startswith("/api/v1/")]
    add(checks, "只发起一次平台请求", len(backend) == 1, f"{len(backend)} 次")
    if not backend:
        return
    item = backend[0]
    add(checks, "请求方法", item["method"] == "GET", item["method"])
    add(checks, "请求路径", item["path"] == path, item["path"])
    add(checks, "查询参数透传", item["params"] == params, json.dumps(item["params"], ensure_ascii=False))
    add(checks, "鉴权头为 Bearer JWT", item["auth"] == "Bearer " + JWT, "Bearer ***" if item["auth"] else "缺失")


def range_checks(codes, scope, stations_by_scope, checks):
    if scope == "business":
        baseline = stations_by_scope.get("baseline")
        if baseline is not None:
            overlap = sorted(set(codes) & set(baseline))
            add(checks, "业务范围不含基线场站", not overlap, str(overlap) if overlap else "无交集")
    elif scope == "baseline":
        baseline = stations_by_scope.get("baseline")
        if baseline is not None:
            extra = sorted(set(codes) - set(baseline))
            add(checks, "基线范围仅含基线场站", not extra, str(extra) if extra else f"{len(codes)} 个")


class StubStore:
    """只记录 Executor 的调用簿记，不连接 Redis/数据库。"""

    security_required = False
    security_gates = True

    def __init__(self):
        self.events = []

    async def check(self, task_id, epoch):
        self.events.append({"event": "check", "task_id": task_id, "epoch": epoch})

    async def begin_call(self, task_id, epoch, step, tool, args, trace_id, asset=None):
        call_id = len(self.events) + 1
        self.events.append({"event": "begin", "call_id": call_id, "tool": tool, "args": args, "trace_id": trace_id})
        return call_id

    async def end_call(self, task_id, epoch, step, call_id, data, error, duration, result):
        self.events.append({"event": "end", "call_id": call_id, "error": error})


def redact(text, extra=()):
    for value in (JWT, *extra):
        if value:
            text = text.replace(value, "[REDACTED]")
    return text


async def run_case(case, index, client, captured, stations_by_scope):
    scope = case["arguments"]["scope"]
    path = "/api/v1/stations" if case["tool"] == "list_stations" else "/api/v1/dashboard/overview"
    entry, match_kind = index.find(path, case["arguments"])
    payload = entry["payload"]
    checks = []
    add(checks, "记录响应 code=0", payload.get("code") == 0, f"code={payload.get('code')} message={payload.get('message')!r}")

    start = len(captured)
    if case["stage"] == 8:
        cfg = RagConfig(model="smoke", llm_url="http://127.0.0.1:18080", llm_key="", service_token="",
                        database_url="", redis_url="", backend_url=str(client.base_url).rstrip("/"), tool_timeout=5)
        result = {"tool_calls": [], "sources": [], "warnings": []}
        data = await Tools(cfg, client, None).execute(case["tool"], json.dumps(case["arguments"], ensure_ascii=False), JWT, result)
    else:
        cfg = SimpleNamespace(backend_url=str(client.base_url).rstrip("/"), service_token="", actor_key=ACTOR_KEY,
                              tool_timeout=5, tool_chars=60000, acceptance_drill_only=False, redact=redact)
        store = StubStore()
        result = {"tool_calls": [], "sources": [], "warnings": []}
        row = {"task_id": 101, "user_id": 7, "trace_id": "smoke-trace"}
        data = await Executor(cfg, store, client).call(row, 1, 1, "platform." + case["tool"], dict(case["arguments"]), JWT, result)
        add(checks, "身份端点已调用", any(item["path"] == "/internal/ma/v1/actor" for item in captured[start:]), "")
        begins = [item for item in store.events if item["event"] == "begin"]
        add(checks, "调用簿记 tool 名称", len(begins) == 1 and begins[0]["tool"] == "platform." + case["tool"],
            begins[0]["tool"] if begins else "缺失")
        add(checks, "调用簿记参数", bool(begins) and begins[0]["args"] == case["arguments"], str(begins[0]["args"]) if begins else "缺失")
        ends = [item for item in store.events if item["event"] == "end"]
        add(checks, "调用簿记无错误", len(ends) == 1 and ends[0]["error"] is None, str(ends[0]["error"]) if ends else "缺失")

    requests = captured[start:]
    request_checks(requests, path, case["arguments"], checks)

    trace = result["tool_calls"][-1] if result["tool_calls"] else {}
    add(checks, "trace 状态 ok", trace.get("status") == "ok", str(trace.get("status")) + (f" {trace.get('error')}" if trace.get("error") else ""))
    add(checks, "trace 参数与请求一致", trace.get("arguments") == case["arguments"], json.dumps(trace.get("arguments", {}), ensure_ascii=False))
    add(checks, "返回数据等于记录 data", data == payload.get("data"), "不一致" if data != payload.get("data") else "一致")
    add(checks, "无工具告警", not result["warnings"], "；".join(result["warnings"]))
    business = [item for item in result["sources"] if item.get("kind") == "business"]
    add(checks, "生成业务来源", len(business) == 1 and business[0].get("url") == path,
        business[0].get("url") if business else "缺失")

    values = scope_values(data)
    add(checks, "返回数据含 scope 标记", bool(values), f"{len(values)} 处")
    if values:
        add(checks, f"scope 标记均为 {scope}", all(value == scope for value in values), "、".join(sorted(set(values))))
    codes = station_codes(data)
    if codes:
        add(checks, "场站编码数量", True, f"{len(codes)} 个：{'、'.join(codes)}")
        range_checks(codes, scope, stations_by_scope, checks)
    else:
        add(checks, "场站编码可核对", False, "返回数据中没有可识别的场站编码")

    return {
        "stage": case["stage"],
        "tool": case["tool"],
        "arguments": case["arguments"],
        "recorded_key": entry["key"],
        "match": match_kind,
        "request": next((item for item in requests if item["path"].startswith("/api/v1/")), None),
        "trace_status": trace.get("status"),
        "data": summarize(data),
        "checks": checks,
        "passed": all(item["ok"] for item in checks),
    }


async def execute(index, backend_url, stations_by_scope):
    captured = []
    handler_state = {}

    def handler(request):
        captured.append({"method": request.method, "path": request.url.path,
                         "params": dict(request.url.params), "auth": request.headers.get("authorization", "")})
        if request.url.path == "/internal/ma/v1/actor":
            return httpx.Response(200, json={"code": 0, "data": {"context": handler_state["token"]}})
        try:
            entry, _ = index.find(request.url.path, dict(request.url.params))
        except LookupError as exc:
            handler_state["error"] = str(exc)
            return httpx.Response(500, json={"code": 50000, "message": str(exc), "data": None})
        return httpx.Response(200, json=entry["payload"])

    claims = ops_security.claims({"user_id": 7, "role": "operator"}, "smoke-session", "smoke-trace", "0" * 64)
    handler_state["token"] = ops_security.sign(claims, ACTOR_KEY)
    reports = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=backend_url) as client:
        for case in CASES:
            reports.append(await run_case(case, index, client, captured, stations_by_scope))
    return reports


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="阶段 8/9 智能体工具离线最小调用验证")
    parser.add_argument("--responses", default=str(DEFAULT_RESPONSES), help="记录的 Go API 响应 JSON")
    parser.add_argument("--backend-url", default="http://127.0.0.1:18080", help="仅用于拼接 URL，由 MockTransport 拦截，不发起真实连接")
    args = parser.parse_args(argv)

    path = Path(args.responses)
    if not path.is_file():
        print(f"响应文件不存在：{path}")
        print("先等待 Codex 生成 api-responses.json，再运行本脚本；本次未执行验证。")
        return 3
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(mapping, dict):
            raise ValueError("顶层必须是对象")
        index = Recorded(mapping)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"响应文件不可用：{path}：{exc}")
        print("文件可能尚未写完或结构不符，本次未执行验证。")
        return 2

    stations_by_scope = {}
    for scope in ("all", "business", "baseline"):
        payload = index.by_path_scope("/api/v1/stations", scope)
        stations_by_scope[scope] = station_codes(payload.get("data")) if payload and payload.get("code") == 0 else None

    reports = asyncio.run(execute(index, args.backend_url.rstrip("/"), stations_by_scope))

    for report in reports:
        status = "通过" if report["passed"] else "失败"
        failed = [item for item in report["checks"] if not item["ok"]]
        print(f"\n[阶段{report['stage']}] {report['tool']} scope={report['arguments']['scope']}：{status}"
              f"（{len(report['checks']) - len(failed)}/{len(report['checks'])} 项）")
        print(f"  记录键：{report['recorded_key']}（{report['match']}匹配）")
        if report["request"]:
            query = "&".join(f"{k}={v}" for k, v in report["request"]["params"].items())
            print(f"  实际请求：GET {report['request']['path']}{'?' + query if query else ''}")
        print(f"  返回摘要：{json.dumps(report['data'], ensure_ascii=False)}")
        for item in failed:
            print(f"  失败：{item['check']}：{item['detail']}")

    passed = sum(1 for report in reports if report["passed"])
    print(f"\n汇总：{passed}/{len(reports)} 个用例通过；网络：未发起真实连接（httpx.MockTransport）；未调用模型。")
    print(json.dumps({"cases": [{k: report[k] for k in ("stage", "tool", "arguments", "recorded_key", "passed", "data")} for report in reports],
                      "passed": passed, "total": len(reports)}, ensure_ascii=False, indent=2))
    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
