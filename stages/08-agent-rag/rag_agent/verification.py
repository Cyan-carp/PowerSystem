"""Explicit, read-only verification. Never called by automatic chat evaluation."""
import asyncio
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .evidence import build_evidence
from .daily import is_program_reply


class Verify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: int = Field(gt=0, strict=True)
    parent_turn_id: int = Field(gt=0, strict=True)
    device_id: int = Field(gt=0, strict=True)
    checks: list[Literal["latest_telemetry", "current_alarms", "prediction"]] = Field(min_length=1, max_length=3)
    verification_id: UUID


async def collect(body, jwt, tools, cfg, result):
    async def call(name, **args):
        if len(result["tool_calls"]) >= cfg.max_calls:
            result["warnings"].append("核验工具调用资源上限已到，保留部分结果。")
            return {"error": "核验工具调用资源上限已到"}
        return await tools.execute(name, json.dumps(args), jwt, result)

    device = await call("get_device", device_id=body.device_id)
    if not isinstance(device, dict) or device.get("error"):
        # A nonexistent or inaccessible device must not launch telemetry queries.
        if isinstance(device, dict) and device.get("error") == "平台接口HTTP 404": raise HTTPException(400, "invalid_device")
        result["verification_results"] = {"device": {"status": "error", "error": "设备台账读取失败，未继续查询。"}}
        return
    if device.get("id") != body.device_id:
        raise HTTPException(400, "invalid_device")
    result["device"] = {"id": device["id"], "device_code": device.get("device_code"), "name": device.get("name")}
    rows = result["verification_results"]
    for check in sorted(set(body.checks)):
        if check != "current_alarms":
            name = "get_telemetry" if check == "latest_telemetry" else "get_prediction"
            data = await call(name, device_id=body.device_id)
            rows[check] = {"status": "error" if "error" in data else "ok", "data": data}
            continue
        alarms = []; complete = True; errors = []; reported_total = 0
        # Save progress even if the total timeout interrupts a later page.
        rows[check] = {"status": "partial", "list": alarms, "complete": False, "errors": errors}
        for status in ("unhandled", "acked"):
            count = 0
            for page in range(1, cfg.verify_max_pages + 1):
                data = await call("list_alarms", device_id=body.device_id, status=status, page=page, page_size=100)
                if not isinstance(data, dict) or not isinstance(data.get("list"), list) or data.get("truncated"):
                    complete = False; errors.append(data.get("error", "告警结果缺失或被截取")); break
                batch = data["list"]
                if not isinstance(data.get("total"), int) or any(not isinstance(r, dict) or r.get("device_id") != body.device_id or r.get("status") not in ("unhandled", "acked") for r in batch):
                    complete = False; errors.append("告警结果格式或设备状态不符"); break
                if page == 1: reported_total += data["total"]
                alarms.extend(batch); count += len(batch)
                if count >= data["total"]: break
                if not batch or page == cfg.verify_max_pages:
                    complete = False; errors.append("告警分页未完整取回"); break
        rows[check].update(status="ok" if complete else "partial", complete=complete, reported_total=reported_total)
        if not complete: result["warnings"].append("当前告警查询不完整，不能据此认定没有告警。")


async def verify_turn(app, sid, body, jwt, ops=None):
    cfg = app.state.config
    if not cfg.verify_enabled: raise HTTPException(503, "verification_disabled")
    # Check ownership before locking or fetching any device data.
    if not await app.state.store.ensure(sid, body.user_id, ""):
        raise HTTPException(404, "session_not_found")
    key, rid = "stage8:session:" + str(sid), str(uuid4())
    if not await app.state.redis.set(key, rid, nx=True, ex=int(cfg.total_timeout + 30)):
        raise HTTPException(409, "session_busy")
    try:
        if not await app.state.store.ensure(sid, body.user_id, ""):
            raise HTTPException(404, "session_not_found")
        turns = await app.state.store.turns(sid)
        parent = next((t for t in turns if t["id"] == body.parent_turn_id and t.get("kind", "chat") == "chat"), None)
        if parent is None:
            raise HTTPException(404, "turn_not_found")
        if is_program_reply(parent.get("response", {})):
            raise HTTPException(400, "verification_not_available_for_turn")
        payload = {"parent_turn_id": body.parent_turn_id, "device_id": body.device_id, "checks": sorted(set(body.checks))}
        existing = await app.state.store.verification(sid, body.verification_id)
        if existing:
            if existing["response"].get("verification_request") != payload:
                raise HTTPException(409, "verification_id_conflict")
            return {**existing["response"], "turn_id": existing["id"]}
        result = {"request_id": rid, "session_id": str(sid), "kind": "verification", "status": "answered",
                  "answer_markdown": "", "model": "程序只读核验（未调用模型）", "sources": [], "tool_calls": [], "warnings": [],
                  "parent_turn_id": body.parent_turn_id, "verification_id": str(body.verification_id),
                  "verification_request": payload, "verification_results": {}, "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            async with asyncio.timeout(cfg.total_timeout):
                if ops is not None:
                    fixed = {"session_id": str(sid), "parent_turn_id": body.parent_turn_id, "device_id": body.device_id,
                             "checks": sorted(set(body.checks)), "verification_id": str(body.verification_id)}
                    task = await ops.dispatch(json.dumps(fixed, ensure_ascii=False), key=body.verification_id, parent=body.parent_turn_id, verification=True)
                    task = await ops.wait(task, cfg.total_timeout-5)
                    if task["status"] not in ("succeeded", "failed", "cancelled", "timeout"):
                        raise HTTPException(503, "verification_pending_retry_same_id")
                    if task.get("error") and task["error"].get("code") == "invalid_device": raise HTTPException(400, "invalid_device")
                    result.update(tool_calls=task["tool_calls"], sources=task["sources"], warnings=task["warnings"],
                                  verification_results=task.get("verification_results") or {})
                    if task.get("device"): result["device"] = task["device"]
                    if task.get("error"): result["warnings"].append(task["error"]["message"])
                else:
                    if not getattr(app.state, "fixture_tools", False): raise HTTPException(503, "ops_identity_unavailable")
                    await collect(body, jwt, app.state.tools, cfg, result)
        except TimeoutError:
            result["warnings"].append("核验超时，保留已取得的部分数据。")
        result["evidence"] = build_evidence(result["tool_calls"], cfg)
        if result["evidence"]["failed_count"] or result["warnings"]:
            result["status"] = "degraded"
        if not cfg.evidence_enabled:
            result.pop("evidence", None)
        question = "主动核验设备 " + str(body.device_id)
        messages = [{"role": "user", "content": question}, {"role": "assistant", "content": "历史只读核验记录（不是当前事实）：" + json.dumps(result, ensure_ascii=False)}]
        result = json.loads(cfg.redact(json.dumps(result, ensure_ascii=False), (jwt,)))
        messages = json.loads(cfg.redact(json.dumps(messages, ensure_ascii=False), (jwt,)))
        result["turn_id"] = await app.state.store.save(sid, question, result, messages, kind="verification", parent_turn_id=body.parent_turn_id, verification_id=body.verification_id)
        return result
    finally:
        from .main import UNLOCK
        await app.state.redis.eval(UNLOCK, 1, key, rid)
