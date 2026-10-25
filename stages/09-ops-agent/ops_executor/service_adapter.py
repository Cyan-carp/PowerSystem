"""Local S07 registered adapter. No DB/model credentials, shell, SQL or remote host.

Execution receipts are a durable journal, never an approval authority. The
stage9 callback revalidates and claims every grant before touching the service.
"""
import asyncio,hashlib,hmac,json,os,socket,sqlite3,subprocess,sys,time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime,timezone
import httpx
from fastapi import FastAPI,HTTPException,Request
from fastapi.responses import JSONResponse
from ops_contract import validate_catalog,digest as registry_digest,safe_logs
from ops_agent.service_contract import POLICY,CAPABILITIES,RECOVERY,digest,verify_grant
from .main import child_environment

def stamp():return datetime.now(timezone.utc).isoformat()

def restricted_identity():
    if os.name!='nt':
        if os.geteuid()==0:raise ValueError('restricted_service_identity_required')
        return dict(os='posix',admin_enabled=False,restricted=True)
    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('advapi32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True);handle=wintypes.HANDLE()
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    api.OpenProcessToken.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.POINTER(wintypes.HANDLE)]
    api.IsTokenRestricted.argtypes=[wintypes.HANDLE];kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    if not api.OpenProcessToken(kernel.GetCurrentProcess(),8,ctypes.byref(handle)):raise ValueError('service_identity_unverifiable')
    try:restricted=bool(api.IsTokenRestricted(handle));admin=bool(ctypes.windll.shell32.IsUserAnAdmin())
    finally:kernel.CloseHandle(handle)
    if admin or not restricted:raise ValueError('restricted_service_identity_required')
    return dict(os='windows',admin_enabled=admin,restricted=restricted)

class LocalService:
    """Only the shipped demo module is registered; argv is never user input."""
    def __init__(self,root,instance):
        self.root=Path(root).resolve();self.instance=instance;self.proc=None;self.generation=0
        self.source=Path(__file__).with_name('demo_service.py').resolve();self.code_hash=hashlib.sha256(self.source.read_bytes()).hexdigest()
    def proof(self):
        return dict(instance=self.instance,pid=self.proc.pid if self.proc else None,generation=self.generation,code_hash=hashlib.sha256(self.source.read_bytes()).hexdigest(),config_hash=digest(dict(adapter='shipped-local-demo/v1',port=28976,instance=self.instance)))
    async def start(self):
        if self.proc and self.proc.poll() is None:raise ValueError('service_already_running')
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',28976))==0:raise ValueError('service_port_occupied')
        if hashlib.sha256(self.source.read_bytes()).hexdigest()!=self.code_hash:raise ValueError('service_code_changed')
        self.root.mkdir(parents=True,exist_ok=True);self.generation+=1
        env={**child_environment(), 'S07_SERVICE_ROOT':str(self.root),'S07_SERVICE_INSTANCE':self.instance,'S07_SERVICE_GENERATION':str(self.generation),'PYTHONIOENCODING':'utf-8'}
        self.proc=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-I',str(self.source)],env=env,cwd=self.root,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for _ in range(40):
            if self.proc.poll() is not None:raise ValueError('service_start_failed')
            try:
                value=await self.health()
                if value['status'] in ('healthy','unhealthy'):return value
            except Exception:pass
            await asyncio.sleep(.05)
        await self.stop()
        raise ValueError('service_health_timeout')
    async def stop(self):
        if self.proc and self.proc.poll() is None:
            # We own this child handle; never resolve or terminate a caller PID.
            self.proc.terminate()
            try:await asyncio.to_thread(self.proc.wait,3)
            except subprocess.TimeoutExpired:
                self.proc.kill();await asyncio.to_thread(self.proc.wait,3)
    async def health(self):
        if not self.proc or self.proc.poll() is not None:return dict(status='stopped',observed_at=stamp())
        async with httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=1) as c:r=await c.get('http://127.0.0.1:28976/health')
        value=r.json()
        if set(value)!={'instance','pid','generation','status'} or value['instance']!=self.instance or value['pid']!=self.proc.pid or value['generation']!=self.generation or value['status'] not in ('healthy','unhealthy'):raise ValueError('service_identity_changed')
        return dict(status=value['status'],observed_at=stamp(),pid=value['pid'],generation=value['generation'])
    def logs(self,args):
        p=self.root/'events.jsonl'
        # Bounded suffix; unknown/free-form fields are discarded by the existing filter.
        with p.open('rb') as f:
            size=f.seek(0,2);f.seek(max(0,size-65536));data=f.read(65536)
        lines=data.decode('utf8','replace').splitlines()
        if size>65536:lines=lines[1:]
        after=time.time()-args['since_seconds'];kept=[]
        for line in lines:
            try:
                v=json.loads(line)
                if type(v.get('ts')) in (int,float) and v['ts']>=after:kept.append(line)
            except (ValueError,TypeError):kept.append('{}')
        return safe_logs('\n'.join(kept),args['limit'],size>65536)

class ServiceConfig:
    def __init__(self,path):
        v=json.loads(Path(path).read_text(encoding='utf8'))
        if set(v)!={'policy','token','grant_key','callback','registry','root','instance'} or v['policy']!=POLICY or v['callback']!='http://127.0.0.1:28973':raise ValueError('isolated_adapter_config_required')
        self.token=v['token'];self.grant_key=v['grant_key'];self.callback=v['callback'];self.registry=v['registry'];self.root=Path(v['root']).resolve();self.instance=v['instance']
        self.identity=restricted_identity()
        if min(len(self.token),len(self.grant_key))<32 or self.token==self.grant_key:raise ValueError('independent_adapter_identity_required')
        self.assets=validate_catalog(self.registry['assets'])
        self.services=[a for a in self.assets.values() if a['kind']=='service']
        if len(self.services)!=1:raise ValueError('single_registered_service_required')
        a=self.services[0]
        if a['environment']!='acceptance' or a['criticality']!='support' or a['connection_ref']!='s07-local-demo' or set(a['capabilities'])!=set(CAPABILITIES):raise ValueError('isolated_registered_service_required')
        self.service=LocalService(self.root/'service',self.instance)
        if a['identity_hash']!=digest(dict(adapter='shipped-local-demo/v1',instance=self.instance,code_hash=self.service.code_hash)):raise ValueError('asset_identity_changed')

def create_app(config):
    @asynccontextmanager
    async def lifespan(app):
        app.state.cfg=config;app.state.lock=asyncio.Lock();app.state.client=httpx.AsyncClient(trust_env=False,follow_redirects=False,timeout=4)
        config.root.mkdir(parents=True,exist_ok=True)
        app.state.journal=sqlite3.connect(config.root/'receipts.sqlite3');app.state.journal.execute('PRAGMA synchronous=FULL');app.state.journal.execute('PRAGMA journal_mode=WAL')
        app.state.journal.execute('CREATE TABLE IF NOT EXISTS receipts(execution_ref TEXT PRIMARY KEY,owner INTEGER NOT NULL,action_id TEXT NOT NULL,run_id TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT)');app.state.journal.commit()
        try:
            await config.service.start()
            yield
        finally:
            await config.service.stop();app.state.journal.close();await app.state.client.aclose()
    app=FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    @app.middleware('http')
    async def auth(req,next_handler):
        if req.url.path.startswith('/internal/') and not hmac.compare_digest(req.headers.get('X-PowerSystem-Token','').encode(),config.token.encode()):return JSONResponse(status_code=401,content={'detail':'invalid_adapter_identity'})
        if req.method=='POST':
            value=await req.body()
            if len(value)>24000:return JSONResponse(status_code=413,content={'detail':'invalid_dispatch'})
        try:return await next_handler(req)
        except HTTPException:raise
        except Exception:return JSONResponse(status_code=503,content={'detail':'adapter_unavailable'})
    def asset(aid):
        a=config.assets.get(aid)
        if not a or a not in config.services or not a['enabled'] or not a['verified_at']:raise HTTPException(403,'registered_service_required')
        return a
    def snapshot(a):
        proof=dict(asset_id=a['asset_id'],asset_version=a['version'],registry_hash=registry_digest(config.registry['assets']),identity_hash=a['identity_hash'],service=config.service.proof())
        return dict(proof=proof,target_version=digest(proof))
    @app.get('/health')
    async def health():return dict(engine='s07-registered-service-adapter',policy=POLICY,status='ok',production_write_enabled=False,service_identity=config.identity,database_credentials=False,model_credentials=False)
    @app.post('/internal/ma-ops-exec/v1/snapshot')
    async def snap(req:Request):
        body=await req.json()
        if not isinstance(body,dict) or set(body)!={'asset_id'}:raise HTTPException(400,'invalid_snapshot')
        return snapshot(asset(body['asset_id']))
    @app.post('/internal/ma-ops-exec/v1/execute')
    async def execute(req:Request):
        body=await req.json()
        if not isinstance(body,dict) or set(body)!={'grant'}:raise HTTPException(400,'invalid_dispatch')
        try:g=verify_grant(body['grant'],config.grant_key)
        except ValueError:raise HTTPException(403,'invalid_grant') from None
        jwt=req.headers.get('Authorization','')
        if not jwt.startswith('Bearer '):raise HTTPException(401,'missing_user_token')
        a=asset(g['typed_args']['asset_id']);write=g['capability']=='service.restart'
        async with app.state.lock:
            old=app.state.journal.execute('SELECT state,receipt FROM receipts WHERE execution_ref=?',(g['execution_ref'],)).fetchone()
            if old:raise HTTPException(409,'execution_already_journaled')
            before=snapshot(a)
            if g['target_version']!=before['target_version']:raise HTTPException(409,'target_version_changed')
            if g['capability'] not in a['capabilities']:raise HTTPException(403,'capability_unavailable')
            if write and (await config.service.health())['status']!='healthy':raise HTTPException(409,'target_not_healthy')
            r=await app.state.client.post(config.callback+'/internal/ma-ops/v1/claims',json=body,headers={'X-MA-Executor-Token':config.token,'Authorization':jwt})
            if r.status_code!=200:raise HTTPException(r.status_code if r.status_code in (401,403,409,503) else 403,'dispatch_not_authorized')
            # The adapter lock protects the actual target recheck and action.
            if snapshot(a)!=before:
                receipt=dict(execution_ref=g['execution_ref'],action_id=g['action_id'],run_id=g['run_id'],capability=g['capability'],policy_version=POLICY,status='failed',no_effect=True,action_confirmed=False,effect_summary='领取后发现目标漂移，未调用服务操作',health_check=dict(status='not_run'),recovery_limit=RECOVERY)
                app.state.journal.execute("INSERT INTO receipts VALUES(?,?,?,?,'completed',?)",(g['execution_ref'],g['owner_id'],g['action_id'],g['run_id'],json.dumps(receipt,ensure_ascii=False)));app.state.journal.commit()
                return receipt
            app.state.journal.execute("INSERT INTO receipts VALUES(?,?,?,?,'started',NULL)",(g['execution_ref'],g['owner_id'],g['action_id'],g['run_id']));app.state.journal.commit()
            receipt=dict(execution_ref=g['execution_ref'],action_id=g['action_id'],run_id=g['run_id'],capability=g['capability'],policy_version=POLICY,status='succeeded',no_effect=not write,action_confirmed=False,before=before,observed_at=stamp(),recovery_limit=RECOVERY)
            try:
                if write:
                    hook=getattr(app.state,'before_restart',None)
                    if hook:await hook()
                    await config.service.stop();await config.service.start();receipt['action_confirmed']=True
                check=await config.service.health();receipt['health_check']=check;receipt['after']=snapshot(a)
                if write and check['status']!='healthy':receipt['status']='failed'
                receipt['effect_summary']=('服务已实际重启；执行后健康检查通过' if check['status']=='healthy' else '服务已实际重启，但执行后健康检查失败；请人工核查') if write else '登记服务诊断/过滤日志已读取；内容仅站内展示'
                if not write:
                    display=dict(asset_id=a['asset_id'],target_version=before['target_version'],health=check,observed_at=stamp(),recovery_limit=RECOVERY)
                    if g['capability'] in ('service.logs','service.evidence'):display['logs']=config.service.logs(g['typed_args'])
                    receipt['display_result']=display
            except Exception:
                receipt.update(status='failed',no_effect=False if write else True,effect_summary='服务操作或健康检查失败；保留实际进程事实，请人工核查',health_check=dict(status='unavailable',observed_at=stamp()))
            app.state.journal.execute("UPDATE receipts SET state='completed',receipt=? WHERE execution_ref=?",(json.dumps(receipt,ensure_ascii=False),g['execution_ref']));app.state.journal.commit()
            hook=getattr(app.state,'after_receipt',None)
            if hook:await hook(receipt)
            return receipt
    @app.get('/internal/ma-ops-exec/v1/receipts/{ref}')
    async def receipt(ref:str,req:Request):
        # The stage9 authority supplies the authenticated owner in a signed,
        # read-only receipt grant, not a client-selected owner parameter.
        from ops_agent.security import verify
        try:ctx=verify(req.headers.get('X-MA-Receipt',''),config.grant_key,'ma-ops-receipt')
        except ValueError:raise HTTPException(403,'invalid_receipt_grant') from None
        if ctx.get('execution_ref')!=ref:raise HTTPException(403,'receipt_binding_changed')
        row=app.state.journal.execute('SELECT owner,state,receipt FROM receipts WHERE execution_ref=?',(ref,)).fetchone()
        if not row or row[0]!=ctx.get('owner_id'):raise HTTPException(404,'receipt_not_found')
        if row[1]!='completed':raise HTTPException(409,'execution_uncertain')
        return json.loads(row[2])
    return app
