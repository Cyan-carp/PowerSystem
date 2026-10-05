import asyncio
import hmac
import time
from contextlib import asynccontextmanager, suppress
from uuid import uuid4
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from .config import Config
from .schemas import InternalChat, ChatResponse
from .sessions import Sessions, SessionError
from .audit import Audit
from .llm import Provider, ProviderError
from .tools import Tools, ToolDenied
from .orchestrator import Orchestrator
from .interpret import GuardedProvider, install_routes
from .knowledge import Knowledge
from .misses import Misses
from .search import Search
from .context import Context


def create_app(config=None, sessions=None, provider=None, tools=None, audit=None):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.load()
        client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        redis = Redis.from_url(cfg.redis_url, decode_responses=True, socket_connect_timeout=2,
                               socket_timeout=2, retry=Retry(NoBackoff(), 0))
        app.state.config = cfg
        app.state.sessions = sessions or Sessions(redis, cfg.session_ttl)
        app.state.redis = redis
        app.state.provider = provider or GuardedProvider(Provider(cfg, client), redis, cfg)
        app.state.tools = tools or Tools(cfg, client)
        app.state.knowledge = Knowledge(cfg.knowledge_path)
        app.state.context = Context(app.state.knowledge, Search(cfg, client), Misses(cfg.misses_dir,
            (cfg.api_key, cfg.service_token, cfg.monitor_token, cfg.search_key)))
        app.state.audit = audit or Audit(cfg.audit_dir, (cfg.api_key, cfg.service_token, cfg.monitor_token, cfg.search_key))
        try:
            yield
        finally:
            await client.aclose()
            await redis.aclose()

    app = FastAPI(title="PowerSystem read-only agent", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_size(request, call_next):
        # Bound JSON before Pydantic parsing; chunked requests are also bounded.
        if request.method == "POST":
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 262144:
                    return JSONResponse(status_code=413, content={"detail": "request_too_large"})
            request._body = bytes(raw)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # FastAPI's default includes raw request values; keep credentials out of errors.
        return JSONResponse(status_code=422, content={"detail": "invalid_request"})

    async def authenticate(request):
        value = request.headers.get("X-PowerSystem-Token", "")
        if not hmac.compare_digest(value.encode(), app.state.config.service_token.encode()):
            try:
                await app.state.audit.write({"request_id": str(uuid4()), "status": "service_token_denied"})
            except OSError:
                raise HTTPException(503, "audit_unavailable") from None
            raise HTTPException(401, "invalid_service_token")

    @app.get("/health")
    async def health():
        return {"status": "ok", "stage": "V2-M3", "model_configured": app.state.config.model_configured, "model_validated": False,
                "knowledge_available": app.state.knowledge.available, "knowledge_version": app.state.knowledge.version,
                "search_configured": bool(app.state.config.search_url and app.state.config.search_key)}

    @app.get("/internal/agent/knowledge/{chunk_id}")
    async def knowledge_source(chunk_id: str, request: Request):
        await authenticate(request)
        chunk = app.state.knowledge.get(chunk_id)
        if not chunk:
            raise HTTPException(404, "knowledge_source_not_found")
        return {**chunk, "version": app.state.knowledge.version}

    @app.post("/internal/agent/probe")
    async def probe(request: Request):
        await authenticate(request)
        try:
            async with asyncio.timeout(app.state.config.total_timeout):
                return await app.state.provider.probe()
        except (ProviderError, TimeoutError) as error:
            reason = str(error) if isinstance(error, ProviderError) else "model_probe_timeout"
            raise HTTPException(503, reason) from None

    async def disconnect_watch(request):
        # FastAPI has consumed the request body. Wait for the actual ASGI
        # disconnect event; cancelling a polling receive scope can miss it.
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    @app.post("/internal/agent/chat", response_model=ChatResponse)
    async def chat(body: InternalChat, request: Request):
        await authenticate(request)
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or len(header) > 8192:
            raise HTTPException(401, "missing_user_token")
        jwt = header[7:]
        if not jwt:
            raise HTTPException(401, "missing_user_token")
        cfg = app.state.config
        message = body.message
        for secret in (jwt, cfg.api_key, cfg.service_token, cfg.monitor_token, cfg.search_key):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        rid, sid = str(uuid4()), None
        started, trace = time.monotonic(), []
        response = ChatResponse(request_id=rid, session_id="", status="degraded", model=cfg.model)
        error = None
        try:
            async with asyncio.timeout(cfg.total_timeout):
                sid, history = await app.state.sessions.acquire(body.session_id, body.user_id, rid)
                response.session_id = sid
                work = asyncio.create_task(Orchestrator(cfg, app.state.provider, app.state.tools).run(
                    message, history, jwt, response, trace, context=app.state.context))
                watcher = asyncio.create_task(disconnect_watch(request))
                try:
                    done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
                    if watcher in done:
                        raise asyncio.CancelledError()
                    response = await work
                finally:
                    for task in (work, watcher):
                        task.cancel()
                        with suppress(asyncio.CancelledError):
                            await task
                await app.state.sessions.save(sid, [*history, {"role": "user", "content": message},
                    {"role": "assistant", "content": response.conclusion.text if response.conclusion else "无法判断"}])
        except SessionError as exc:
            error = (exc.status, exc.reason)
        except ToolDenied as exc:
            error = (exc.status, "backend_authorization_denied")
        except (TimeoutError, ProviderError) as exc:
            response.status = "degraded"
            response.conclusion = None
            response.suggestions = []
            response.limitations = ["无法判断：" + (str(exc) if isinstance(exc, ProviderError) else "agent_timeout")]
        except asyncio.CancelledError:
            error = (499, "request_cancelled")
            raise
        finally:
            if sid:
                try:
                    await app.state.sessions.release(sid, rid)
                except SessionError as exc:
                    error = (exc.status, exc.reason)
            try:
                await app.state.audit.write({"request_id": rid, "session_id": sid, "user_id": body.user_id,
                    "elapsed_ms": round((time.monotonic()-started)*1000), "calls": trace,
                    "status": error[1] if error else response.status, "response": response.model_dump()}, secrets=(jwt,))
            except OSError:
                error = (503, "audit_unavailable")
        if error:
            raise HTTPException(*error)
        return response

    install_routes(app, authenticate)
    return app


app = create_app()
