"""Service-token protected API. Public identity is supplied by the Go JWT gateway."""
import asyncio
import hashlib
import json
from uuid import UUID
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .ma_security import verify, digest
from .run_worker import AuthVault,run_profile
from .run_store import TERMINAL, PROTOCOL, encode, unpack


class AcceptRun(BaseModel):
    model_config=ConfigDict(extra='forbid')
    user_id:int=Field(gt=0,strict=True)
    message:str=Field(min_length=1,max_length=32000)
    session_id:UUID|None=None
    page_context:dict|None=None


def routes(app):
    from .actions import routes as action_routes
    action_routes(app)
    def auth(request):
        token=request.headers.get('Authorization','')
        if not token.startswith('Bearer ') or not token[7:]:raise HTTPException(401,'missing_user_token')
        return token[7:]

    @app.post('/internal/agent/runs',status_code=202)
    async def accept(body:AcceptRun,request:Request):
        cfg=app.state.config
        if not cfg.v4_runs_enabled or not cfg.v4_assistant_enabled:raise HTTPException(503,'runs_disabled')
        jwt=auth(request)
        try:key=UUID(request.headers.get('Idempotency-Key',''))
        except ValueError:raise HTTPException(400,'invalid_idempotency_key') from None
        message=body.message.strip()
        if not message or len(message)>cfg.input_chars:raise HTTPException(400,'invalid_message')
        if body.page_context is not None and len(encode(body.page_context))>8000:raise HTTPException(400,'invalid_page_context')
        actor=None
        if cfg.ma_actor_key:
            try:
                claims=verify(request.headers.get('X-MA-Actor',''),cfg.ma_actor_key,'ma-ops')
                if claims['user_id']!=body.user_id or claims['request_hash']!=hashlib.sha256(body.message.encode()).hexdigest():raise ValueError()
                if claims['session_id']!=(str(body.session_id) if body.session_id else ''):raise ValueError()
                actor={k:claims[k] for k in ('user_id','role')}
            except (ValueError,KeyError):raise HTTPException(403,'invalid_actor') from None
        # Never persist plaintext tokens. Run auth is encrypted with a domain-separated service-key derivation.
        sealed=AuthVault(cfg.service_token).seal(body.user_id,dict(jwt=jwt,actor=actor))
        safe_message=cfg.redact(message,(jwt,))
        page=json.loads(cfg.redact(encode(body.page_context),(jwt,)))
        hashed=digest(dict(protocol=PROTOCOL,message=message,session_id=str(body.session_id) if body.session_id else None,page_context=body.page_context))
        result=await app.state.runs.accept(body.user_id,key,safe_message,body.session_id,page,run_profile(cfg),sealed,cfg.total_timeout,request_hash=hashed,actor=actor)
        return result

    @app.get('/internal/agent/runs/{rid}')
    async def snapshot(rid:UUID,user_id:int,request:Request):
        jwt=auth(request)
        value=await app.state.runs.snapshot(rid,user_id)
        if value.get('requires_manual_reconciliation') and value.get('actions'):
            # An owner explicitly reopening a manual-reconciliation result may
            # learn late execution facts. Preserve the original partial/manual
            # terminal outcome; never run the graph or dispatch another action.
            try:
                row=unpack(await app.state.runs.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 AND user_id=$2',rid,user_id))
                state=await app.state.run_worker.authority_state(row,cancel=False,jwt=jwt)
                await app.state.runs.project_authority(rid,user_id,state.get('actions',[]))
                value=await app.state.runs.snapshot(rid,user_id)
            except Exception:
                value['authority_sync_status']='pending_reconciliation'
        return value

    @app.get('/internal/agent/runs/{rid}/events')
    async def events(rid:UUID,user_id:int,request:Request):
        auth(request)
        query=request.query_params.get('after_seq')
        header=request.headers.get('Last-Event-ID')
        def parse(value):
            if value is None:return None
            if not value.isascii() or not value.isdigit() or len(value)>19:raise HTTPException(400,'invalid_event_cursor')
            result=int(value)
            if result>2**63-1:raise HTTPException(400,'invalid_event_cursor')
            return result
        after,last=parse(query),parse(header)
        if after is not None and last is not None and after!=last:raise HTTPException(400,'event_cursor_conflict')
        cursor=after if after is not None else last if last is not None else 0
        # Validate before StreamingResponse commits status/headers.
        batch,terminal=await app.state.runs.event_batch(rid,user_id,cursor)
        async def stream():
            nonlocal batch,terminal,cursor
            heartbeat=asyncio.get_running_loop().time()
            while True:
                for event in batch:
                    cursor=event['seq']
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {encode(event)}\n\n"
                if terminal and len(batch)<200:return
                if await request.is_disconnected():return
                if asyncio.get_running_loop().time()-heartbeat>=15:
                    yield ': heartbeat\n\n';heartbeat=asyncio.get_running_loop().time()
                await asyncio.sleep(.1)
                try:batch,terminal=await app.state.runs.event_batch(rid,user_id,cursor)
                except HTTPException:return  # Reconnect re-validates an event retention boundary.
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.post('/internal/agent/runs/{rid}/cancel')
    async def cancel(rid:UUID,user_id:int,request:Request):
        auth(request)
        snapshot=await app.state.runs.cancel(rid,user_id)
        if snapshot['actions']:
            try:
                row=unpack(await app.state.runs.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1',rid))
                state=await app.state.run_worker.authority_state(row,cancel=snapshot['status'] not in TERMINAL,jwt=auth(request))
                if state.get('pending'):raise ValueError()
                snapshot['actions']=state.get('actions',[])
            except Exception:
                if snapshot['status'] in TERMINAL:
                    return JSONResponse(status_code=202,content={**snapshot,'authority_sync_status':'pending_reconciliation'})
                return JSONResponse(status_code=202,content={**snapshot,'status':'pending_reconciliation','last_known_status':snapshot['status']})
        return snapshot
