import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from ops_contract import canonical, digest, validate_catalog, validate_dispatch
from ops_write_contract import verify as verify_write, snapshot_grant, proof, matches


class AdapterConfig:
    def __init__(self,path):
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        self.token=Path(value['token_file']).read_text(encoding='utf-8-sig').strip()
        self.grant_key=Path(value['grant_key_file']).read_text(encoding='utf-8-sig').strip()
        self.registry=json.loads(Path(value['registry_file']).read_text(encoding='utf-8-sig'))
        self.callback_url=value['callback_url'];self.hosts=value['hosts'];self.ssh=value['ssh_executable']
        self.drill_url=value.get('drill_url')
        self.security_required=value.get('security_required',False)
        if type(self.security_required) is not bool: raise ValueError('invalid_security_switch')
        self.write_key=Path(value['write_key_file']).read_text(encoding='utf-8-sig').strip() if value.get('write_key_file') else ''
        if self.drill_url:
            parsed=urlsplit(self.drill_url)
            if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or parsed.path or parsed.username or parsed.query or parsed.fragment or len(self.write_key)<32: raise ValueError('invalid_drill_origin')
        if min(len(self.token),len(self.grant_key))<32: raise ValueError('adapter_credentials_required')
        origin=urlsplit(self.callback_url)
        if origin.scheme!='http' or origin.hostname not in ('127.0.0.1','ops-agent') or origin.username or origin.path or origin.query or origin.fragment:
            raise ValueError('invalid_callback_origin')
        validate_catalog(self.registry['assets'])
        if not Path(self.ssh).is_absolute() or not Path(self.ssh).is_file(): raise ValueError('invalid_ssh_executable')
        for host in self.hosts.values():
            if set(host)!={'hostname','port','private_key_file','known_hosts_file'}: raise ValueError('invalid_transport')
            if not isinstance(host['hostname'],str) or not __import__('re').fullmatch('[A-Za-z0-9.-]+',host['hostname']): raise ValueError('invalid_transport')
            if type(host['port']) is not int or not 1<=host['port']<=65535: raise ValueError('invalid_transport')
            for key in ('private_key_file','known_hosts_file'):
                if not Path(host[key]).is_absolute() or not Path(host[key]).is_file(): raise ValueError('invalid_transport')


def child_environment():
    # Windows OpenSSH exits 255 without stderr if its OS/user environment is removed.
    # Inherit only these OS fields; never LLM, database, grant/token or application variables.
    if os.name=='nt':
        keys=('SYSTEMROOT','WINDIR','PATH','USERPROFILE','USERNAME','USERDOMAIN','TEMP','TMP','PROGRAMDATA','LOCALAPPDATA','APPDATA','COMSPEC')
        return {k:os.environ[k] for k in keys if k in os.environ}
    return {'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}


async def ssh_collect(cfg,asset,body):
    host_asset=asset if asset['kind']=='host' else next(a for a in cfg.registry['assets'] if a['asset_id']==asset['parent_id'])
    host=cfg.hosts.get(host_asset['connection_ref'])
    if not host: raise ValueError('asset_not_verified')
    argv=[cfg.ssh,'-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','UpdateHostKeys=no',
          '-o','IdentitiesOnly=yes','-o','IdentityAgent=none','-o','ClearAllForwardings=yes',
          '-o','ConnectTimeout=4','-o','UserKnownHostsFile='+host['known_hosts_file'],
          '-i',host['private_key_file'],'-p',str(host['port']),'powersystem-ops-read@'+host['hostname']]
    process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,env=child_environment())
    try:
        process.stdin.write(canonical(body));await process.stdin.drain();process.stdin.close()
        stdout=bytearray()
        async with asyncio.timeout(14):
            while True:
                block=await process.stdout.read(8192)
                if not block: break
                stdout.extend(block)
                if len(stdout)>262144: raise ValueError('invalid_result')
            if await process.wait()!=0: raise ValueError('target_unreachable')
        return json.loads(stdout)
    finally:
        if process.returncode is None: process.kill();await process.wait()


def create_app(config=None,transport=None,callback_client=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.cfg=config or AdapterConfig(os.environ['OPS_EXECUTOR_CONFIG_FILE'])
        app.state.client=callback_client or httpx.AsyncClient(follow_redirects=False,trust_env=False,timeout=4)
        try: yield
        finally:
            if not callback_client: await app.state.client.aclose()

    app=FastAPI(title='PowerSystem R0 execution adapter',lifespan=lifespan)

    @app.middleware('http')
    async def guard(req,next_handler):
        if req.url.path.startswith('/internal/') and not hmac.compare_digest(req.headers.get('X-MA-Executor-Token','').encode(),app.state.cfg.token.encode()):
            return JSONResponse(status_code=401,content={'detail':'invalid_adapter_token'})
        if req.method=='POST':
            chunks=[];size=0
            async for chunk in req.stream():
                size+=len(chunk)
                if size>65536: return JSONResponse(status_code=413,content={'detail':'invalid_dispatch'})
                chunks.append(chunk)
            req._body=b''.join(chunks)
        try: return await next_handler(req)
        except (HTTPException,asyncio.CancelledError): raise
        except Exception: return JSONResponse(status_code=503,content={'detail':'adapter_unavailable'})

    @app.get('/health')
    async def health(): return {'engine':'restricted-adapter' if getattr(app.state.cfg,'drill_url',None) else 'r0-adapter','status':'ok','write_tools_registered':bool(getattr(app.state.cfg,'drill_url',None))}

    @app.post('/internal/ma-exec/v2/snapshot')
    async def snapshot(req:Request):
        cfg=app.state.cfg;body=await req.json()
        fields={'asset_id','gate'} if getattr(cfg,'security_required',False) else {'asset_id'}
        if not getattr(cfg,'drill_url',None) or not isinstance(body,dict) or set(body)!=fields: raise HTTPException(403,'guard_blocked')
        from ops_contract import asset_id
        try: aid=asset_id(body['asset_id'])
        except ValueError: raise HTTPException(400,'invalid_args') from None
        assets={a['asset_id']:a for a in cfg.registry['assets']}
        if aid not in assets or assets[aid]['criticality']!='drill' or not assets[aid]['enabled'] or assets[aid]['environment'] not in ('acceptance','fixture'): raise HTTPException(403,'guard_blocked')
        if getattr(cfg,'security_required',False):
            callback=await app.state.client.post(cfg.callback_url+'/internal/ma/v1/security-snapshots/claim',json={'gate':body['gate']},headers={'X-MA-Executor-Token':cfg.token})
            if callback.status_code!=200: raise HTTPException(403,'security_snapshot_not_authorized')
        r=await app.state.client.post(cfg.drill_url+'/internal/drill/v1/snapshot',json={'grant':snapshot_grant(aid,cfg.write_key)})
        if r.status_code!=200: raise HTTPException(409,'asset_version_changed')
        data=r.json()
        if set(data)!={'snapshot','policy'} or digest(data['policy'])!=proof(data['snapshot'])['policy_version'] or data['snapshot']['registry_hash']!=digest(cfg.registry['assets']): raise HTTPException(503,'invalid_result')
        return data

    @app.post('/internal/ma-exec/v2/writes')
    async def write(req:Request):
        cfg=app.state.cfg;body=await req.json()
        if not getattr(cfg,'drill_url',None) or not isinstance(body,dict) or set(body)!={'grant'}: raise HTTPException(403,'guard_blocked')
        try: g=verify_write(body['grant'],cfg.write_key)
        except ValueError: raise HTTPException(403,'invalid_write_grant') from None
        if g['action']!='restart' or g['registry_hash']!=digest(cfg.registry['assets']): raise HTTPException(403,'guard_blocked')
        if getattr(cfg,'security_required',False):
            callback=await app.state.client.post(cfg.callback_url+'/internal/ma/v1/write-grants/claim',json=body,headers={'X-MA-Executor-Token':cfg.token})
            if callback.status_code!=200: raise HTTPException(callback.status_code if callback.status_code in (403,409,503) else 403,'dispatch_not_authorized')
        # Snapshot recheck happens before the authoritative one-shot database consumption.
        r=await app.state.client.post(cfg.drill_url+'/internal/drill/v1/snapshot',json={'grant':snapshot_grant(g['asset_id'],cfg.write_key)})
        if r.status_code!=200 or not matches(g,proof(r.json()['snapshot'])): raise HTTPException(409,'approval_invalidated')
        if not getattr(cfg,'security_required',False):
            callback=await app.state.client.post(cfg.callback_url+'/internal/ma/v1/write-grants/claim',json=body,headers={'X-MA-Executor-Token':cfg.token})
            if callback.status_code!=200: raise HTTPException(callback.status_code if callback.status_code in (403,409,503) else 403,'dispatch_not_authorized')
        # Nothing after consumption is ever retried. Missing responses conservatively mean uncertain.
        try:
            r=await app.state.client.post(cfg.drill_url+'/internal/drill/v1/restart',json=body,timeout=15)
            if r.status_code!=200:
                return {'status':'error','error':{'code':'execution_uncertain','message':'execution_uncertain','retryable':False},'data':{}}
            data=r.json()
            if set(data)!={'status','before','after','action_confirmed'} or data['status']!='ok' or data['action_confirmed'] is not True or not matches(g,proof(data['before'])) or not matches(g,proof(data['after'])) or data['after']['health']!='healthy' or not data['after']['running'] or data['before']['started_at']==data['after']['started_at']:
                raise ValueError('invalid_result')
            return {'status':'ok','data':data,'error':None}
        except Exception:
            return {'status':'error','data':{},'error':{'code':'execution_uncertain','message':'execution_uncertain','retryable':False}}

    @app.get('/internal/ma-exec/v1/registry')
    async def registry():
        assets=app.state.cfg.registry['assets']
        return {'assets':assets,'registry_hash':digest(assets)}

    @app.post('/internal/ma-exec/v1/calls')
    async def dispatch(req:Request):
        cfg=app.state.cfg
        try:
            body=await req.json();grant,args,asset=validate_dispatch(body,cfg.grant_key,cfg.registry)
        except (ValueError,KeyError,TypeError): raise HTTPException(403,'guard_blocked') from None
        # Authoritative one-shot consumption lives in ma_ops, not this stateless adapter.
        try:
            r=await app.state.client.post(cfg.callback_url+'/internal/ma/v1/execution-grants/claim',
                json={'grant':body['grant']},headers={'X-MA-Executor-Token':cfg.token})
        except httpx.HTTPError: raise HTTPException(503,'authorization_unavailable') from None
        if r.status_code!=200: raise HTTPException(r.status_code if r.status_code in (403,409,503) else 403,'dispatch_not_authorized')
        try:
            async with asyncio.timeout(min(15,max(.001,grant['deadline']-__import__('time').time()))):
                if getattr(cfg,'drill_url',None) and asset['connection_ref']=='local-drill':
                    local=await app.state.client.post(cfg.drill_url+'/internal/drill/v1/read',json=body,timeout=14)
                    if local.status_code!=200: raise ValueError('target_unreachable')
                    reply=local.json()
                else:
                    reply=await (transport(cfg,asset,body) if transport else ssh_collect(cfg,asset,body))
            if not isinstance(reply,dict) or set(reply)!={'status','data','error'} or reply['status'] not in ('ok','error') or not isinstance(reply['data'],dict):
                raise ValueError('invalid_result')
            # Shared output contract rejects raw text/extra fields even from a faulty collector.
            from ops_contract import validate_result
            validate_result(body['tool'],reply)
            return reply
        except (TimeoutError,OSError):
            return {'status':'error','data':{},'error':{'code':'target_unreachable','message':'target_unreachable','retryable':True}}
        except ValueError as exc:
            code=str(exc) if str(exc) in ('asset_not_verified','target_unreachable','invalid_result') else 'invalid_result'
            return {'status':'error','data':{},'error':{'code':code,'message':code,'retryable':code=='target_unreachable'}}

    return app


app=create_app()
