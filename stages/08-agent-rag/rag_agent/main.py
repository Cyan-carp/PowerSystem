import asyncio
import hmac
import json
import re
import time
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from .config import Config
from .engine import Engine
from .knowledge import Knowledge
from .provider import Provider
from .store import Store
from .tools import Tools
from .evidence import build_evidence
from .verification import Verify, verify_turn
from .daily import classify, model_history, is_program_reply, RULE_VERSION
from .ops import Ops, SupervisorTools
from .ma_security import verify as verify_context


class Chat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    session_id: UUID | None = None
    user_id: int = Field(gt=0, strict=True)


UNLOCK = "if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"


def create_app(config=None, store=None, redis=None, provider=None, tools=None, run_worker=True, run_authority=None):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.load()
        app.state.config = cfg
        client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        app.state.client = client
        app.state.store = store or await Store.open(cfg.database_url)
        app.state.redis = redis or Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=3)
        app.state.knowledge = Knowledge(cfg, client)
        # Pay the local model startup cost before accepting chat requests.
        # Missing model files remain a recoverable tool failure, not a chat outage.
        app.state.embedding_ready = False
        if tools is None:
            try:
                await asyncio.to_thread(app.state.knowledge.embedding.encode, ["启动检查"], True)
                app.state.embedding_ready = True
            except Exception:
                pass
        app.state.tools = tools or Tools(cfg, client, app.state.knowledge)
        app.state.fixture_tools = tools is not None
        app.state.provider = provider or Provider(cfg, client)
        loop=None;schedule_loop=None
        if hasattr(app.state.store,'pool'):
            from .run_store import RunStore
            from .run_worker import RunWorker
            app.state.runs=RunStore(app.state.store)
            from .artifacts import ArtifactService
            app.state.artifacts=ArtifactService(app.state.runs,cfg,client)
            app.state.run_worker=RunWorker(app,app.state.runs,run_authority)
            from .schedules import ScheduleService
            app.state.schedules=ScheduleService(app)
            if getattr(cfg,'v4_schedules_enabled',False):
                await app.state.schedules.guard()
                if run_worker:schedule_loop=asyncio.create_task(app.state.schedules.loop())
            # Keep recovery and queries alive even when new admission is disabled.
            if run_worker:loop=asyncio.create_task(app.state.run_worker.loop())
        try:
            yield
        finally:
            if schedule_loop:
                schedule_loop.cancel();await asyncio.gather(schedule_loop,return_exceptions=True)
            if hasattr(app.state,'schedules'):
                for task in list(app.state.schedules.controls):task.cancel()
                await asyncio.gather(*list(app.state.schedules.controls),return_exceptions=True)
            if loop:
                loop.cancel();await asyncio.gather(loop,return_exceptions=True)
                await app.state.run_worker.stop()
            await client.aclose()
            if not store: await app.state.store.pool.close()
            if not redis: await app.state.redis.aclose()

    app = FastAPI(title="PowerSystem general Agent and RAG", lifespan=lifespan)
    from .schedules import routes as schedule_routes
    schedule_routes(app)
    from .schedule_bridge import routes as bridge_routes
    bridge_routes(app)

    @app.middleware("http")
    async def security(request, next_handler):
        if request.url.path.startswith("/internal/") and request.url.path != "/internal/agent/knowledge/search":
            supplied = request.headers.get("X-PowerSystem-Token", "")
            expected = app.state.config.service_token
            if request.url.path.startswith('/internal/schedule-bridge/'):
                expected = getattr(app.state.config,'schedule_bridge_token','')
            if len(expected)<32 or not hmac.compare_digest(supplied.encode(), expected.encode()):
                return JSONResponse(status_code=401, content={"detail": "invalid_service_token"})
        if request.method == "POST":
            raw = bytearray()
            async for part in request.stream():
                raw.extend(part)
                if len(raw) > 256 * 1024:
                    return JSONResponse(status_code=413, content={"detail": "request_too_large"})
            request._body = bytes(raw)
        return await next_handler(request)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse(status_code=422, content={"detail": "invalid_request"})

    @app.get("/health")
    async def health():
        return {"status": "ok", "engine": "agent-rag", "model": app.state.config.model, "model_validated": False,
                "embedding_ready": app.state.embedding_ready, "boundaries": {"evidence": app.state.config.evidence_enabled, "verify": app.state.config.verify_enabled,
                "daily_gate": app.state.config.daily_gate_enabled, "daily_rule_version": RULE_VERSION,
                "v4_assistant": getattr(app.state.config, 'v4_assistant_enabled', False)}}

    @app.get("/internal/agent/sessions")
    async def sessions(user_id: int, offset: int = 0):
        if user_id <= 0 or offset < 0: raise HTTPException(400, "invalid_query")
        return {"list": await app.state.store.list(user_id, offset), "offset": offset, "page_size": 50}

    @app.get("/internal/agent/sessions/{sid}")
    async def details(sid: UUID, user_id: int, request: Request):
        if not await app.state.store.ensure(sid, user_id, ""):
            raise HTTPException(404, "session_not_found")
        turns = await app.state.store.turns(sid)
        if app.state.config.ma_actor_key:
            ops = Ops(app.state.config, app.state.client, request.headers.get("X-MA-Actor", ""), request.headers.get("Authorization", "").removeprefix("Bearer "), sid, user_id)
            for turn in turns:
                for card in turn["response"].get("task_cards", []):
                    if turn.get('run_id'):continue  # S03 children are already bound at creation.
                    if card.get("historical") or not card.get("link_pending"): continue
                    try:
                        await ops.bind(card["task_id"], turn["id"])
                        card["link_pending"] = False
                    except HTTPException: pass
        for turn in turns:
            turn["response"]["verification_available"] = app.state.config.verify_enabled and not is_program_reply(turn["response"])
            if app.state.config.evidence_enabled and "evidence" not in turn["response"]:
                turn["response"]["evidence"] = build_evidence(turn["response"].get("tool_calls", []), app.state.config)
            if not app.state.config.evidence_enabled:
                turn["response"].pop("evidence", None)
        return {"session_id": str(sid), "turns": [{k: v for k, v in t.items() if k != "model_messages"} for t in turns]}

    @app.post("/internal/agent/sessions/{sid}/verify")
    async def verify(sid: UUID, body: Verify, request: Request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:]: raise HTTPException(401, "missing_user_token")
        raw_question = request.headers.get("X-MA-Request", "")
        if raw_question:
            import base64
            raw_question = base64.urlsafe_b64decode(raw_question + "=" * (-len(raw_question)%4)).decode()
        ops = Ops(app.state.config, app.state.client, request.headers.get("X-MA-Actor", ""), auth[7:], sid, body.user_id, raw_question) if app.state.config.ma_actor_key else None
        return await verify_turn(app, sid, body, auth[7:], ops)

    @app.delete("/internal/agent/sessions/{sid}")
    async def delete(sid: UUID, user_id: int, request: Request):
        key, rid = "stage8:session:" + str(sid), str(uuid4())
        if not await app.state.redis.set(key, rid, nx=True, ex=150):
            raise HTTPException(409, "session_busy")
        try:
            if not await app.state.store.ensure(sid, user_id, ""):
                raise HTTPException(404, "session_not_found")
            ops = Ops(app.state.config, app.state.client, request.headers.get("X-MA-Actor", ""), request.headers.get("Authorization", "").removeprefix("Bearer "), sid, user_id)
            reserved = False
            if app.state.config.ma_actor_key:
                await ops.guard(rid, "reserve"); reserved = True
            deleted = False
            try:
                if not await app.state.store.delete(sid, user_id): raise HTTPException(404, "session_not_found")
                deleted = True
                if reserved: await ops.guard(rid, "commit")
            finally:
                if reserved and not deleted: await ops.guard(rid, "release")
            return {"deleted": True}
        finally:
            await app.state.redis.eval(UNLOCK, 1, key, rid)

    @app.get("/internal/agent/rag/sources/{chunk_id}")
    async def source(chunk_id: str, collection: str | None = None, version: str | None = None):
        if not re.fullmatch(r"K[a-f0-9]{20}", chunk_id): raise HTTPException(400, "invalid_source")
        try:
            row = await app.state.knowledge.source(chunk_id,collection,version) if collection or version else await app.state.knowledge.source(chunk_id)
        except ValueError:
            raise HTTPException(400,'invalid_source_reference') from None
        if not row: raise HTTPException(404, "source_not_found")
        return row

    @app.post("/internal/agent/knowledge/search")
    async def ops_knowledge(request: Request):
        cfg = app.state.config
        if not cfg.ops_token or not hmac.compare_digest(request.headers.get("X-Ops-Token", "").encode(), cfg.ops_token.encode()):
            raise HTTPException(403, "invalid_ops_token")
        from .tools import KnowledgeQuery
        try: body = KnowledgeQuery.model_validate(await request.json())
        except Exception: raise HTTPException(400, "invalid_query") from None
        return {"results": await app.state.knowledge.search(body.query, body.limit)}

    @app.post("/internal/agent/chat")
    async def chat(body: Chat, request: Request):
        cfg = app.state.config
        if not body.message.strip() or len(body.message) > cfg.input_chars:
            raise HTTPException(400, "invalid_message")
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or not auth[7:]: raise HTTPException(401, "missing_user_token")
        jwt = auth[7:]
        question = cfg.redact(body.message, (jwt,))
        sid, rid = body.session_id or uuid4(), str(uuid4())
        actor_token = request.headers.get("X-MA-Actor", "")
        if cfg.ma_actor_key:
            try:
                actor = verify_context(actor_token, cfg.ma_actor_key, "ma-ops")
                if actor["user_id"] != body.user_id or actor["session_id"] != str(sid) or actor["request_hash"] != hashlib.sha256(body.message.encode()).hexdigest(): raise ValueError()
            except (ValueError, KeyError): raise HTTPException(403, "invalid_actor") from None
        ops = Ops(cfg, app.state.client, actor_token, jwt, sid, body.user_id, body.message)
        key = "stage8:session:" + str(sid)
        if not await app.state.redis.set(key, rid, nx=True, ex=int(cfg.total_timeout + 30)):
            raise HTTPException(409, "session_busy")
        result = {"request_id": rid, "session_id": str(sid), "status": "degraded", "answer_markdown": "",
                  "sources": [], "tool_calls": [], "warnings": [], "model": cfg.model,
                  "response_mode": "model", "model_called": False}
        try:
            create_session = body.session_id is None or request.headers.get("X-MA-New-Session") == "true"
            if not await app.state.store.ensure(sid, body.user_id, question, create=create_session):
                raise HTTPException(404, "session_not_found")
            previous = await app.state.store.turns(sid)
            try:
                legacy_daily = cfg.daily_gate_enabled and not getattr(cfg, 'v4_assistant_enabled', False)
                decision = classify(question, previous) if legacy_daily else None
                context = model_history(previous, legacy_daily)
            except Exception:
                raise HTTPException(503, "daily_boundary_unavailable") from None
            if decision is not None:
                result["boundary"] = decision.metadata()
            if decision is not None and decision.action != "allow":
                result.update(status="answered" if decision.action == "preset" else "out_of_scope",
                              answer_markdown=decision.reply(), model="",
                              response_mode="preset" if decision.action == "preset" else "boundary")
                messages = []  # Display history persists; program replies are never model context.
            else:
                supervisor_tools = SupervisorTools(app.state.tools, ops, time.monotonic()+cfg.total_timeout)
                work = asyncio.create_task(Engine(cfg, app.state.provider, supervisor_tools).run(
                    question, context, jwt, result))
                async def watch_disconnect():
                    while True:
                        if (await request.receive())["type"] == "http.disconnect": return
                watch = asyncio.create_task(watch_disconnect())
                try:
                    done, _ = await asyncio.wait((work, watch), return_when=asyncio.FIRST_COMPLETED)
                    if watch in done and work not in done:
                        raise asyncio.CancelledError()
                    messages = await work
                finally:
                    for task in (work, watch):
                        task.cancel()
                        try: await task
                        except asyncio.CancelledError: pass
            result["sources"] = list({(s["kind"], s["url"]): s for s in result["sources"]}.values())
            if cfg.evidence_enabled:
                result["evidence"] = build_evidence(result["tool_calls"], cfg)
            result["verification_available"] = cfg.verify_enabled and not is_program_reply(result)
            result = json.loads(cfg.redact(json.dumps(result, ensure_ascii=False), (jwt,)))
            messages = json.loads(cfg.redact(json.dumps(messages, ensure_ascii=False), (jwt,)))
            result["turn_id"] = await app.state.store.save(sid, question, result, messages)
            for task_card in result.get("task_cards", []):
                if task_card.get("historical"): continue
                try:
                    await ops.bind(task_card["task_id"], result["turn_id"])
                    task_card["link_pending"] = False
                    task_card["parent_turn_id"] = result["turn_id"]
                except HTTPException:
                    result["warnings"].append("任务关联待重试，任务不会重复创建。")
            try:
                cfg.audit_dir.mkdir(parents=True, exist_ok=True)
                with (cfg.audit_dir / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl")).open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"user_id": body.user_id, **result}, ensure_ascii=False) + "\n")
            except OSError:
                result["warnings"].append("调用日志暂不可写；会话已保存。")
            return result
        finally:
            await app.state.redis.eval(UNLOCK, 1, key, rid)

    from .run_api import routes
    routes(app)
    from .artifact_api import routes as artifact_routes
    artifact_routes(app)
    return app


app = create_app()
