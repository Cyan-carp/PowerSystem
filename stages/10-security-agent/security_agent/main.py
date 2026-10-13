import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from review_contract import POLICY, Decision, ReviewRequest, digest, projection, sign_message, validate_opinion, verify_message
from .config import Config
from .provider import Provider, ReviewFailure


def create_app(config=None, provider=None):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.load()
        cfg.validate()
        app.state.cfg = cfg
        app.state.active = set()
        app.state.users = set()
        app.state.seen = {}
        client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        app.state.provider = provider or Provider(cfg, client)
        try: yield
        finally: await client.aclose()

    app = FastAPI(title='PowerSystem independent Security Agent', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def authenticate(req, next_handler):
        if req.url.path.startswith('/internal/'):
            if not hmac.compare_digest(req.headers.get('X-SEC-Service-Token', '').encode(), app.state.cfg.service_token.encode()):
                return JSONResponse(status_code=401, content={'detail': 'invalid_security_service_token'})
            chunks = []; total = 0
            async for chunk in req.stream():
                total += len(chunk)
                if total > 65536: return JSONResponse(status_code=413, content={'detail': 'review_request_too_large'})
                chunks.append(chunk)
            req._body = b''.join(chunks)
        try: return await next_handler(req)
        except (HTTPException, asyncio.CancelledError): raise
        except Exception: return JSONResponse(status_code=503, content={'detail': 'security_unavailable'})

    @app.exception_handler(RequestValidationError)
    async def invalid(req, exc):
        return JSONResponse(status_code=400, content={'detail': 'security_invalid'})

    @app.get('/health')
    async def health():
        cfg = app.state.cfg
        return {'engine': 'security-agent', 'enabled': cfg.enabled, 'model': cfg.model,
                'model_timeout_seconds': cfg.model_timeout, 'acceptance_timeout_exception': cfg.acceptance_timeout_exception,
                'model_validated': False, 'policy_version': POLICY, 'execution_tools': False}

    @app.post('/internal/ma-review/v1/reviews')
    async def review(body: ReviewRequest, req: Request):
        cfg = app.state.cfg
        if not cfg.enabled: raise HTTPException(503, 'security_disabled')
        if body.policy_version!=POLICY and (cfg.model!='deepseek-v4-flash-0731' or cfg.model_timeout!=30 or cfg.acceptance_timeout_exception):
            raise HTTPException(503,'runtime_model_configuration_blocked')
        raw = body.model_dump(mode='json')
        try:
            signature = verify_message(req.headers.get('X-SEC-Request', ''), cfg.request_key,
                                       'ma-review-request/v1', body.review_id, digest(raw))
            if signature['exp'] > int(body.deadline.timestamp()): raise ValueError()
        except ValueError: raise HTTPException(403, 'invalid_review_signature') from None
        try: projected = projection(body, cfg.secrets())
        except ValueError: raise HTTPException(400, 'security_projection_blocked') from None
        remaining = body.deadline.timestamp() - time.time()
        if remaining <= 0: raise HTTPException(409, 'security_review_stale')
        if body.review_id in app.state.active: raise HTTPException(409, 'security_review_in_progress')
        now = time.time()
        app.state.seen = {rid: until for rid, until in app.state.seen.items() if until > now}
        if body.review_id in app.state.seen: raise HTTPException(409, 'security_review_replayed')
        if len(app.state.seen) >= 1024: raise HTTPException(429, 'security_capacity')
        if body.actor.user_id in app.state.users or len(app.state.active) >= cfg.global_concurrency:
            raise HTTPException(429, 'security_capacity', headers={'Retry-After': '1'})
        app.state.active.add(body.review_id); app.state.users.add(body.actor.user_id)
        app.state.seen[body.review_id] = signature['exp']
        start = time.monotonic()
        try:
            async with asyncio.timeout(min(cfg.model_timeout, remaining)):
                value, usage = await app.state.provider.review(projected, min(cfg.model_timeout, remaining))
            try:
                opinion=validate_opinion(value,body,cfg.secrets())
                result=Decision(protocol='ma-review/v1',review_id=body.review_id,**opinion.model_dump(mode='json'))
            except ValueError as exc:
                # Only stable validation categories; never log model text or sensitive inputs.
                messages = [item['msg'] for item in exc.errors(include_input=False,include_context=False,include_url=False)] if isinstance(exc,ValidationError) else [str(exc)]
                categories = ('invalid_rule_hits','invalid_evidence_ref','review_correlation_mismatch',
                              'sensitive_material_blocked','duplicate_evidence_ref')
                category = next((code for code in categories if any(code in message for message in messages)), 'decision_schema_invalid')
                raise ReviewFailure('model_invalid_decision', 'invalid', category) from None
            if time.time() >= body.deadline.timestamp(): raise ReviewFailure('security_review_stale')
            response = result.model_dump(mode='json')
            bound = {'request_hash': digest(raw), 'decision_hash': digest(response),
                     'usage': usage, 'duration_ms': int((time.monotonic() - start) * 1000)}
            # Metadata shares the authenticated response header; only the six-field decision is in the body.
            import base64
            from review_contract import canonical
            metadata = base64.urlsafe_b64encode(canonical(bound)).decode()
            expiry = min(int(body.deadline.timestamp()), int(time.time()) + cfg.review_ttl)
            return JSONResponse(content=response, headers={'X-SEC-Metadata': metadata,
                'X-SEC-Result': sign_message(cfg.result_key, 'ma-review-result/v1', body.review_id, digest(bound), expiry)})
        except (TimeoutError, ReviewFailure) as exc:
            code = exc.code if isinstance(exc, ReviewFailure) else 'model_timeout'
            state = exc.state if isinstance(exc, ReviewFailure) else 'unavailable'
            headers={'X-SEC-Validation':exc.validation_code} if isinstance(exc,ReviewFailure) and exc.validation_code else None
            raise HTTPException(422 if state == 'invalid' else 503, code,headers=headers) from None
        finally:
            app.state.active.discard(body.review_id); app.state.users.discard(body.actor.user_id)

    return app


app = create_app()
