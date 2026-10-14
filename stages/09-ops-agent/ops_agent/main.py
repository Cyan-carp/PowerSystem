import asyncio
import hmac
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID
import httpx
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from redis.asyncio import Redis
from .config import Config
from .security import verify, digest
from .schemas import Task, Bind, Guard, Verification, STATUS, TERMINAL, AssetImport, ExecutionClaim, ApprovalDecision
from ops_contract import verify_grant
from .store import Store
from .worker import Worker


def create_app(config=None, store=None, redis=None, provider=None, run_worker=True):
    @asynccontextmanager
    async def lifespan(app):
        cfg = config or Config.load(); cfg.validate()
        app.state.cfg = cfg
        app.state.store = store or await Store.open(cfg)
        app.state.redis = redis or Redis.from_url(cfg.redis_url, decode_responses=True, socket_timeout=3)
        app.state.contexts = {}
        singleton = await app.state.store.pool.acquire()
        if not await singleton.fetchval("SELECT pg_try_advisory_lock(90902)"):
            await app.state.store.pool.release(singleton)
            raise RuntimeError("ops_single_process_required")
        client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        app.state.worker = Worker(cfg, app.state.store, app.state.redis, client, app.state.contexts, provider)
        restore = cfg.runtime_profile == "m4-restore-readonly"
        if not restore:
            if app.state.store.security_gates:
                await app.state.store.security_gates.controller.recover()
            await app.state.store.recover(startup=True)
        loop = asyncio.create_task(app.state.worker.loop()) if run_worker and not restore else None
        try: yield
        finally:
            if loop: loop.cancel(); await asyncio.gather(loop, return_exceptions=True)
            await app.state.worker.stop()
            await client.aclose()
            await singleton.execute("SELECT pg_advisory_unlock(90902)")
            await app.state.store.pool.release(singleton)
            if not store: await app.state.store.pool.close()
            if not redis: await app.state.redis.aclose()

    app = FastAPI(title="PowerSystem read-only Operation Agent", lifespan=lifespan)

    @app.middleware("http")
    async def security(req, next_handler):
        cfg = app.state.cfg
        if cfg.runtime_profile == "m4-restore-readonly" and req.method not in ("GET", "HEAD"):
            return JSONResponse(status_code=403, content={"detail":"restore_read_only"})
        if req.url.path in ('/internal/ma/v1/execution-grants/claim','/internal/ma/v1/write-grants/claim','/internal/ma/v1/security-snapshots/claim'):
            if not cfg.infra_enabled or not cfg.executor_token or not hmac.compare_digest(req.headers.get('X-MA-Executor-Token','').encode(),cfg.executor_token.encode()):
                return JSONResponse(status_code=403,content={'detail':'invalid_adapter_token'})
        elif req.url.path.startswith("/internal/"):
            if not hmac.compare_digest(req.headers.get("X-PowerSystem-Token", "").encode(), cfg.service_token.encode()):
                return JSONResponse(status_code=401, content={"detail": "invalid_service_token"})
            try:
                req.state.actor = verify(req.headers.get("X-MA-Actor", ""), cfg.actor_key, "ma-ops")
                if type(req.state.actor["user_id"]) is not int or req.state.actor["user_id"] <= 0 or req.state.actor["role"] not in ("operator", "admin"): raise ValueError()
            except (ValueError, KeyError): return JSONResponse(status_code=403, content={"detail": "invalid_actor"})
        if req.method == "POST":
            chunks = []; size = 0
            async for chunk in req.stream():
                size += len(chunk)
                if size > 256*1024: return JSONResponse(status_code=413, content={"detail": "request_too_large"})
                chunks.append(chunk)
            req._body = b"".join(chunks)
        try: return await next_handler(req)
        except (HTTPException, asyncio.CancelledError): raise
        except ValueError as exc:
            if str(exc) in ('security_denied','security_clarification_required','security_invalid','security_review_stale','security_projection_blocked'):
                return JSONResponse(status_code=409,content={'detail':str(exc)})
            return JSONResponse(status_code=503,content={'detail':'ops_dependency_unavailable'})
        except Exception: return JSONResponse(status_code=503, content={"detail": "ops_dependency_unavailable"})

    @app.exception_handler(RequestValidationError)
    async def invalid(req, exc):
        code = "unsupported_protocol" if any(e.get("loc") == ("body", "protocol") for e in exc.errors()) else "invalid_args"
        return JSONResponse(status_code=400, content={"detail": code})

    @app.get("/health")
    async def health():
        ready = True
        try: await app.state.store.pool.fetchval("SELECT 1"); await app.state.redis.ping()
        except Exception: ready = False
        return {"engine": "ops-agent", "status": "ok" if ready else "degraded", "enabled": app.state.cfg.enabled, 'infrastructure_enabled':app.state.cfg.infra_enabled, "write_tools_registered": app.state.cfg.write_enabled, "model": app.state.cfg.model, "model_validated": False}

    @app.post('/internal/ma/v1/assets/import')
    async def import_assets(body:AssetImport,req:Request):
        cfg=app.state.cfg
        if not cfg.infra_enabled: raise HTTPException(503,'infrastructure_disabled')
        if req.state.actor['role']!='admin': raise HTTPException(403,'admin_required')
        jwt=req.headers.get('Authorization','')
        if not jwt.startswith('Bearer '): raise HTTPException(401,'missing_user_token')
        actor=await app.state.worker.executor.identity(jwt[7:])
        if actor['user_id']!=req.state.actor['user_id'] or actor['role']!='admin': raise HTTPException(403,'admin_required')
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=4) as client:
            r=await client.get(cfg.executor_url+'/internal/ma-exec/v1/registry',headers={'X-MA-Executor-Token':cfg.executor_token})
        if r.status_code!=200: raise HTTPException(503,'registry_unavailable')
        registry=r.json()
        if registry.get('registry_hash')!=body.registry_hash: raise HTTPException(409,'registry_mismatch')
        return await app.state.store.import_assets(registry['assets'],body.registry_hash,actor['user_id'])

    @app.post('/internal/ma/v1/execution-grants/claim')
    async def claim_execution(body:ExecutionClaim):
        cfg=app.state.cfg
        try: grant=verify_grant(body.grant,cfg.executor_grant_key)
        except ValueError: raise HTTPException(403,'invalid_execution_grant') from None
        ctx=app.state.contexts.get(UUID(grant['task_id']))
        if ctx is None: raise HTTPException(403,'auth_context_lost')
        actor=await app.state.worker.executor.identity(ctx['jwt'])
        if actor['user_id']!=grant['user_id'] or actor['role']!=grant['role']: raise HTTPException(403,'identity_changed')
        return await app.state.store.claim_execution(grant)

    @app.post('/internal/ma/v1/write-grants/claim')
    async def claim_write(body:ExecutionClaim):
        from ops_write_contract import verify as verify_write
        cfg=app.state.cfg
        if not cfg.enabled or not cfg.write_enabled: raise HTTPException(403,'write_disabled')
        try: grant=verify_write(body.grant,cfg.executor_write_key)
        except ValueError: raise HTTPException(403,'invalid_write_grant') from None
        ctx=app.state.contexts.get(UUID(grant['task_id']))
        if ctx is None: raise HTTPException(403,'auth_context_lost')
        actor=await app.state.worker.executor.identity(ctx['jwt'])
        if actor['user_id']!=grant['user_id'] or actor['role']!='admin': raise HTTPException(403,'identity_changed')
        return await app.state.store.consume_write(grant)

    @app.post('/internal/ma/v1/security-snapshots/claim')
    async def claim_snapshot(req: Request):
        cfg=app.state.cfg;body=await req.json()
        if not cfg.security_enabled or not isinstance(body,dict) or set(body)!={'gate'}:
            raise HTTPException(403,'security_invalid')
        try:
            g=verify(body['gate'],cfg.executor_grant_key,'ma-security-snapshot')
            if set(g)!={'aud','kid','iat','exp','task_id','user_id','execution_epoch','step_no','asset_id'} or g['exp']-g['iat']>30: raise ValueError()
            tid=UUID(g['task_id'])
        except (ValueError,TypeError): raise HTTPException(403,'security_invalid') from None
        ctx=app.state.contexts.get(tid)
        if not ctx: raise HTTPException(403,'auth_context_lost')
        actor=await app.state.worker.executor.identity(ctx['jwt'])
        if actor['user_id']!=g['user_id']: raise HTTPException(403,'identity_changed')
        async with app.state.store.pool.acquire() as c,c.transaction():
            task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            app.state.store._write_or_read_context(task,g['execution_epoch'])
            if task['user_id']!=g['user_id']: raise HTTPException(403,'security_invalid')
            step=await c.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 AND step_no=$2',tid,g['step_no'])
            if not step or step['tool']!='docker.inspect_container' or step['asset_id']!=g['asset_id']: raise HTTPException(403,'security_invalid')
            rid=await app.state.store.security_gates.validate_in_tx(c,tid,g['step_no'],g['execution_epoch'],'sensitive_read',step['args_hash'])
            reviewed=await c.fetchval('SELECT request FROM ma_security_reviews WHERE review_id=$1',rid)
            if json.loads(reviewed)['actor']!={k:actor[k] for k in ('user_id','role')}:raise HTTPException(403,'identity_changed')
            inserted=await c.fetchval('INSERT INTO ma_security_snapshot_claims(grant_hash,task_id,execution_epoch) VALUES($1,$2,$3) ON CONFLICT DO NOTHING RETURNING grant_hash',digest(g),tid,g['execution_epoch'])
            if inserted is None: raise HTTPException(409,'dispatch_replayed')
        return {'authorized':True}

    def owner(req): return req.state.actor["user_id"]

    async def report_for(req,tid):
        report=await app.state.store.report(tid,owner(req))
        for a in report['approvals']:
            a['can_decide']=app.state.cfg.enabled and app.state.cfg.write_enabled and req.state.actor['role']=='admin' and report['status']=='awaiting_approval' and a['status']=='pending' and datetime.fromisoformat(a['expires_at'].replace('Z','+00:00'))>datetime.now(timezone.utc)
            if app.state.store.security_required:
                from .runtime_reviews import task_policy
                row=await app.state.store.get(tid,owner(req))
                a['can_decide']=a['can_decide'] and app.state.store.security_gates is not None and a['plan'].get('security_policy')==task_policy(row,app.state.cfg)[0]
        return report

    def match(req, sid):
        if req.state.actor["session_id"] != str(sid): raise HTTPException(403, "context_mismatch")

    async def accept(task, req, verification=None):
        cfg = app.state.cfg
        if not cfg.enabled: raise HTTPException(503, "ops_disabled")
        if task.task_kind=='security_inspection' and (not cfg.inspection_enabled or not cfg.security_enabled or verification is not None):
            raise HTTPException(403,'inspection_disabled')
        actor = req.state.actor
        match(req, task.session_id)
        if task.actor.user_id != owner(req) or task.actor.role != actor["role"] or str(task.trace_id) != actor["trace_id"] or hashlib.sha256(task.user_request.encode()).hexdigest() != actor["request_hash"]:
            raise HTTPException(403, "context_mismatch")
        raw = task.model_dump(mode="json")
        try:
            signed = verify(req.headers.get("X-MA-Envelope", ""), cfg.envelope_key, "ma-envelope")
            if signed["hash"] != digest(await req.json()): raise ValueError()
        except (ValueError, KeyError): raise HTTPException(403, "invalid_envelope_signature") from None
        token = req.headers.get("Authorization", "")
        if not token.startswith("Bearer ") or not token[7:]: raise HTTPException(401, "missing_user_token")
        current = await app.state.worker.executor.identity(token[7:])
        if current["user_id"] != owner(req) or current["role"] != actor["role"]: raise HTTPException(403, "identity_changed")
        if task.operation=='restart_drill_container' or task.constraints.max_risk_class=='R2':
            from ops_write_contract import explicit_restart
            if not cfg.enabled or not cfg.write_enabled: raise HTTPException(403,'write_disabled')
            if current['role']!='admin': raise HTTPException(403,'admin_required')
            if task.operation!='restart_drill_container' or task.constraints.max_risk_class!='R2' or not explicit_restart(task.user_request): raise HTTPException(400,'invalid_write_intent')
        await app.state.redis.ping()
        tid, new = await app.state.store.create(task)
        if new: app.state.contexts[tid] = {"jwt": token[7:], "verification": verification}
        return await app.state.store.report(tid, owner(req))

    @app.post("/internal/ma/v1/tasks", status_code=202)
    async def create(body: Task, req: Request): return await accept(body, req)

    @app.get("/internal/ma/v1/tasks")
    async def listing(req: Request, session_id: UUID | None = None, status: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        if status is not None and status not in STATUS: raise HTTPException(400, "invalid_status")
        return await app.state.store.listing(owner(req), session_id, status, offset, limit)

    @app.get("/internal/ma/v1/tasks/{tid}")
    async def detail(tid: UUID, req: Request): return await report_for(req,tid)

    @app.post('/internal/ma/v1/tasks/{tid}/model-summary')
    async def model_summary(tid:UUID,req:Request):
        if await req.json() != {}: raise HTTPException(400,'invalid_args')
        store=app.state.store
        report=await report_for(req,tid)
        if not store.security_gates: raise HTTPException(503,'security_unavailable')
        jwt=req.headers.get('Authorization','')
        if not jwt.startswith('Bearer '): raise HTTPException(401,'missing_user_token')
        from .runtime_reviews import licensed_summary
        payload=licensed_summary(report, app.state.cfg)
        row=await store.get(tid,owner(req))
        steps=await store.pool.fetch('SELECT * FROM ma_task_steps WHERE task_id=$1 AND asset_id IS NOT NULL ORDER BY step_no DESC',tid)
        if not steps and report.get('task_kind')=='security_inspection':
            steps=await store.pool.fetch('SELECT * FROM ma_task_steps WHERE task_id=$1 ORDER BY step_no DESC LIMIT 1',tid)
        if not steps: raise HTTPException(409,'security_invalid')
        step=steps[0]
        try:
            rid=await store.security_gates.require(app.state.worker.executor,row,row['execution_epoch'],step['step_no'],step['tool'],json.loads(step['args']),jwt[7:],row['result'],'data_egress',payload=payload,receiver='supervisor',history=True)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None
        return {'payload':payload,'payload_hash':digest(payload),'review_id':str(rid)}

    @app.post('/internal/ma/v1/tasks/{tid}/model-summary/check')
    async def check_model_summary(tid:UUID,req:Request):
        from .reviews import ControlFailure
        body=await req.json()
        if not isinstance(body,dict) or set(body)!={'review_id','payload_hash'}:raise HTTPException(400,'invalid_args')
        store=app.state.store
        await store.get(tid,owner(req))
        if not store.security_gates:raise HTTPException(503,'security_unavailable')
        jwt=req.headers.get('Authorization','')
        if not jwt.startswith('Bearer '):raise HTTPException(401,'missing_user_token')
        actor=await app.state.worker.executor.identity(jwt[7:])
        if actor['user_id']!=owner(req):raise HTTPException(403,'context_mismatch')
        try:
            rid=UUID(body['review_id'])
            review=await store.security_gates.controller.get(rid,owner(req))
            if review['request']['actor']!={k:actor[k] for k in ('user_id','role')}:raise ValueError('security_review_stale')
            await store.security_gates.check_egress(tid,rid,owner(req),body['payload_hash'],'supervisor')
        except (ValueError,ControlFailure):raise HTTPException(409,'security_review_stale') from None
        return {'permitted':True}

    @app.post("/internal/ma/v1/tasks/{tid}/cancel")
    async def cancel(tid: UUID, req: Request): return await app.state.store.cancel(tid, owner(req))

    @app.post('/internal/ma/v1/tasks/{tid}/approvals/{aid}/decision')
    async def decision(tid:UUID,aid:UUID,body:ApprovalDecision,req:Request):
        a=await app.state.store.approval(tid,aid,owner(req))
        jwt=req.headers.get('Authorization','')
        if not jwt.startswith('Bearer '): raise HTTPException(401,'missing_user_token')
        actor=await app.state.worker.executor.identity(jwt[7:])
        if actor['user_id']!=owner(req) or actor['role']!='admin': raise HTTPException(403,'admin_required')
        current=None
        if body.decision=='approve' and a['status']=='pending':
            from .write import snapshot
            if app.state.cfg.security_enabled:
                # Actual snapshot is gated in the newly claimed execution epoch.
                current=a['plan']['snapshot']
            else:
                try: current=(await snapshot(app.state.worker.executor,a['plan']['target']))['snapshot']
                except ValueError: current={}
        # Install fresh ephemeral authentication before the approval transaction can publish queued.
        previous=app.state.contexts.get(tid)
        fresh={'jwt':jwt[7:],'verification':None} if body.decision=='approve' and a['status']=='pending' else None
        if fresh is not None:app.state.contexts[tid]=fresh
        try:
            changed=await app.state.store.decide(tid,aid,owner(req),body.decision,current)
        except Exception:
            if fresh is not None and app.state.contexts.get(tid) is fresh:
                if previous is None:app.state.contexts.pop(tid,None)
                else:app.state.contexts[tid]=previous
            raise
        if fresh is not None and not changed and app.state.contexts.get(tid) is fresh:
            if previous is None:app.state.contexts.pop(tid,None)
            else:app.state.contexts[tid]=previous
        return await report_for(req,tid)

    @app.post("/internal/ma/v1/tasks/{tid}/bind-turn")
    async def bind(tid: UUID, body: Bind, req: Request):
        match(req, body.session_id)
        # Only the Supervisor possesses the independently authenticated envelope key.
        try:
            signed = verify(req.headers.get("X-MA-Envelope", ""), app.state.cfg.envelope_key, "ma-envelope")
            if signed["hash"] != digest({"task_id": str(tid), **body.model_dump(mode="json")}): raise ValueError()
        except (ValueError, KeyError): raise HTTPException(403, "invalid_binding_signature") from None
        return await app.state.store.bind(tid, owner(req), body.session_id, body.parent_turn_id)

    @app.post("/internal/ma/v1/sessions/{sid}/guard")
    async def guard(sid: UUID, body: Guard, req: Request):
        match(req, sid)
        return await app.state.store.guard(sid, owner(req), body.operation_id, body.action)

    @app.post("/internal/ma/v1/verifications")
    async def verification(req: Request):
        # Same signed task envelope, with a fixed-flow payload inside intent.
        raw = await req.json()
        try:
            body = Task.model_validate(raw)
            payload = Verification.model_validate(json.loads(body.intent))
        except Exception: raise HTTPException(400, "invalid_verification") from None
        if body.task_kind!='ops': raise HTTPException(400,'invalid_verification')
        if payload.session_id != body.session_id or payload.parent_turn_id != body.parent_turn_id or payload.verification_id != body.idempotency_key: raise HTTPException(400, "invalid_verification")
        return await accept(body, req, payload)

    return app


app = create_app()
