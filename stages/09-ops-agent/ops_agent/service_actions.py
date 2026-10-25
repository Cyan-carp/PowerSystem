"""S07 uses the existing stage9 authority and tables under an isolated policy."""
import json,time
from datetime import datetime,timezone
from urllib.parse import urlsplit
from uuid import UUID,uuid4
from fastapi import HTTPException,Request
from .business_actions import BusinessAuthority,unpack,now
from .business_contract import Decision
from . import service_contract as contract
from .security import verify,digest as envelope_digest
from .store import encode

class ServiceAuthority(BusinessAuthority):
    protocol=contract.PROTOCOL;policy=contract.POLICY;capabilities=contract.CAPABILITIES
    parse_arguments=staticmethod(contract.arguments);build_binding=staticmethod(contract.binding)
    audience='ma-ops-execute/v1';adapter_path='/internal/ma-ops-exec/v1'
    @property
    def adapter_url(self):return self.cfg.executor_url
    @property
    def grant_key(self):return self.cfg.executor_write_key
    def verify_token(self,token):return contract.verify_grant(token,self.grant_key)
    def adapter_headers(self,jwt):return {'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.executor_token}
    def receipt_headers(self,task,attempt,jwt):
        from .security import sign
        at=int(time.time())
        return {**self.adapter_headers(jwt),'X-MA-Receipt':sign(dict(aud='ma-ops-receipt',kid='ma-v1',iat=at,exp=at+30,owner_id=task['user_id'],execution_ref=str(attempt['call_id'])),self.grant_key)}
    def policy_snapshot(self):return dict(profile=self.policy,r2=['service.restart'],sensitive=['service.logs','service.evidence'],production=False,adapters=['shipped-local-demo/v1'])
    def automatic(self,capability,opinion):return capability!='service.restart' and opinion['decision']=='allow'
    async def guard(self):
        from v4_joint import selected, guard
        if selected(self.cfg):
            try:await guard(self.cfg,self.pool,'ops')
            except ValueError:raise HTTPException(403,'isolated_s09_policy_required') from None
            return
        cfg=self.cfg;db=urlsplit(cfg.database_url)
        if not cfg.v4_ops_enabled:raise HTTPException(403,'ops_actions_disabled')
        if db.hostname!='127.0.0.1' or db.port!=25437 or not db.path.startswith('/v4_s07_') or not db.path.endswith('_test') or cfg.backend_url!='http://127.0.0.1:28972' or cfg.security_url!='http://127.0.0.1:28974' or cfg.executor_url!='http://127.0.0.1:28975' or cfg.runtime_profile!='legacy' or cfg.write_enabled or cfg.v4_actions_enabled or len(cfg.executor_write_key)<32:raise HTTPException(403,'isolated_s07_policy_required')
        name=await self.pool.fetchval('SELECT current_database()')
        if not name.startswith('v4_s07_') or not name.endswith('_test'):raise HTTPException(403,'isolated_s07_database_required')
    async def identity(self,jwt,owner,role=None):
        return await super().identity(jwt,owner,role)
    async def propose(self,body,actor,jwt):
        if body.capability=='service.restart' and actor['role']!='admin':raise HTTPException(403,'admin_required')
        if body.capability=='service.restart':
            from ops_write_contract import explicit_restart
            if not explicit_restart(body.user_request):raise HTTPException(400,'explicit_restart_required')
        return await super().propose(body,actor,jwt)
    async def registered(self,aid,cap):
        a=await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',aid)
        if not a or a['kind']!='service' or not a['enabled'] or not a['verified_at'] or a['environment'] not in ('acceptance','fixture') or a['criticality'] not in ('support','drill') or cap not in json.loads(a['capabilities']):raise HTTPException(409,'registered_service_required')
        parent=await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',a['parent_id'])
        if not parent or not parent['enabled'] or not parent['verified_at'] or parent['registry_hash']!=a['registry_hash'] or parent['environment'] not in ('acceptance','fixture'):raise HTTPException(409,'registered_service_required')
        return dict(a)
    async def snapshot(self,cap,args,jwt):
        a=await self.registered(args['asset_id'],cap)
        r=await self.client.post(self.adapter_url+self.adapter_path+'/snapshot',json={'asset_id':args['asset_id']},headers={'X-PowerSystem-Token':self.cfg.executor_token},timeout=3)
        if r.status_code!=200:raise HTTPException(409,'target_unavailable')
        snap=r.json();proof=snap['proof']
        if (proof['asset_version'],proof['registry_hash'],proof['identity_hash'])!=(a['version'],a['registry_hash'],a['identity_hash']) or contract.digest(proof)!=snap['target_version']:raise HTTPException(409,'target_version_changed')
        return dict(target_version=snap['target_version'],target_summary=a['display_name']+' · '+a['asset_id'],proof=proof)
    async def review(self,tid,owner,plan):
        from .runtime_reviews import contract as rc
        rid=uuid4();b=plan['binding'];ordinary=b['capability']=='service.diagnose'
        raw=dict(protocol='ma-ops-review/v1',review_id=str(rid),binding_hash=plan['plan_hash'],capability=b['capability'],data_class=b['data_class'],destination=b['destination_id'],risk_class=plan['risk_class'],owner_id=owner,policy_version=self.policy,target_version=b['target_version'],argument_hash=contract.digest(b['typed_args']),expires_at=int(datetime.fromisoformat(b['expires_at']).timestamp()))
        await self.pool.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,1,$2,$3,1,'v4_ops',$4,$5,$6::jsonb,'pending',$7)",rid,tid,owner,rc.digest(raw),plan['plan_hash'],encode(raw),'deterministic_catalog' if ordinary else 'deepseek-flash')
        try:
            if ordinary:
                op=dict(protocol='ma-ops-review/v1',review_id=str(rid),binding_hash=plan['plan_hash'],decision='allow',rule_codes=['ordinary_read'],expires_at=raw['expires_at']);usage={};duration=0
            else:
                r=await self.client.post(self.cfg.security_url+'/internal/ma-ops-review/v1/reviews',json=raw,headers={'X-SEC-Service-Token':self.cfg.security_service_token,'X-SEC-Request':rc.sign_message(self.cfg.security_request_key,'ma-ops-review-request/v1',rid,rc.digest(raw),raw['expires_at'])},timeout=32)
                if r.status_code!=200:raise ValueError()
                payload=r.json();op=payload['decision'];usage=payload.get('usage',{});duration=payload.get('duration_ms')
                rc.verify_message(r.headers.get('X-SEC-Result',''),self.cfg.security_result_key,'ma-ops-review-result/v1',rid,rc.digest(op))
            if op['protocol']!='ma-ops-review/v1' or op['review_id']!=str(rid) or op['binding_hash']!=plan['plan_hash'] or op['decision'] not in ('allow','require_approval','deny') or not time.time()<op['expires_at']<=raw['expires_at']:raise ValueError()
            await self.pool.execute("UPDATE ma_security_reviews SET state='completed',decision=$2::jsonb,decision_hash=$3,signature_validated=true,expires_at=$4,usage=$5::jsonb,duration_ms=$6,finished_at=now() WHERE review_id=$1",rid,encode(op),rc.digest(op),datetime.fromtimestamp(op['expires_at'],timezone.utc),encode(usage),duration)
            return op
        except Exception:
            await self.pool.execute("UPDATE ma_security_reviews SET state='unavailable',error_code='security_unavailable',finished_at=now() WHERE review_id=$1",rid)
            raise HTTPException(503,'security_unavailable') from None
        finally:await self.pool.execute('INSERT INTO ma_security_review_events(review_id,state,reason) SELECT review_id,state,coalesce(error_code,state) FROM ma_security_reviews WHERE review_id=$1',rid)
    async def validate_plan(self,task,a,jwt):
        await super().validate_plan(task,a,jwt)
        b=a['plan']['binding'];env=task['envelope']
        expected=self.build_binding(task['user_id'],contract.Proposal.model_validate({k:env[k] for k in contract.Proposal.model_fields}),self.parse_arguments(env['capability'],env['typed_args']),b['target_version'],b['expires_at'])
        if b!=expected or a['policy_version']!=self.policy or b['destination_id']!='platform_client' or a['plan']['risk_class']!=self.capabilities[b['capability']][0]:raise HTTPException(409,'approval_binding_changed')
        if b['capability']=='service.restart' and a['plan']['actor_role']!='admin':raise HTTPException(403,'admin_required')
    async def claim(self,token,jwt):
        try:g=self.verify_token(token)
        except ValueError:raise HTTPException(403,'invalid_grant') from None
        task=await self.get_task(UUID(g['action_id']),g['owner_id']);a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',task['task_id']))
        if not a:raise HTTPException(409,'approval_missing')
        await self.validate_plan(task,a,jwt)
        return await super().claim(token,jwt)
    def validate_evidence(self,evidence,task,cid):
        super().validate_evidence(evidence,task,cid)
        if evidence.get('policy_version')!=self.policy or evidence.get('capability')!=task['envelope']['capability'] or evidence.get('status') not in ('succeeded','failed') or type(evidence.get('no_effect')) is not bool:raise ValueError('invalid_ops_receipt')
        if evidence['capability']=='service.restart' and evidence['status']=='succeeded' and (not evidence.get('action_confirmed') or evidence.get('health_check',{}).get('status')!='healthy'):raise ValueError('invalid_health_receipt')
    def execution_status(self,evidence):return evidence['status']
    async def projection(self,aid,owner,c=None):
        db=c or self.pool;task=await self.get_task(aid,owner,db);a=unpack(await db.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid));attempt=unpack(await db.fetchrow('SELECT * FROM ma_execution_attempts WHERE task_id=$1',aid));cap=task['envelope']['capability']
        if not a:return dict(protocol=self.protocol,action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),capability=cap,risk_class=self.capabilities[cap][0],status='unknown' if task['status']=='planning' else 'blocked',authority_revision=1,observed_at=now().isoformat(),stale=False,error_code=(task['result'].get('error') or {}).get('code'),effect_summary='审查尚未完成' if task['status']=='planning' else '操作已阻断，未执行',target_summary='')
        p=a['plan'];b=p['binding'];e=(attempt or {}).get('evidence') or {};write=cap=='service.restart'
        # Full bounded parameters are display metadata, never a grant or a
        # caller-editable decision body.
        psummary=json.dumps(b['typed_args'],ensure_ascii=False,sort_keys=True)
        return dict(protocol=self.protocol,action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),approval_id=str(a['approval_id']),plan_hash=p['plan_hash'],expires_at=a['expires_at'].isoformat(),risk_class=p['risk_class'],status=attempt['status'] if attempt else a['status'],approval_status=a['status'],consumed_at=a['consumed_at'].isoformat() if a['consumed_at'] else None,execution_ref=str(attempt['call_id']) if attempt else None,authority_revision=a['authority_revision'],observed_at=now().isoformat(),stale=False,effect_summary=e.get('effect_summary') or ('重启单个登记隔离服务，短暂中断；执行后核对健康，不自动回滚' if write else '读取登记服务诊断/过滤日志；内容仅站内展示'),target_summary=p['target_summary'],target_version=b['target_version'],capability=cap,typed_args=b['typed_args'],parameters_summary=psummary,policy_version=self.policy,destination_id=b['destination_id'],recovery_limit=contract.RECOVERY,error_code=(task['result'].get('error') or {}).get('code'),display_result=json.dumps(e['display_result'],ensure_ascii=False,indent=2) if e.get('display_result') is not None else None,health_check=json.dumps(e['health_check'],ensure_ascii=False) if e.get('health_check') else None,action_confirmed=e.get('action_confirmed'),no_effect=e.get('no_effect',False))

def routes(app):
    def svc():return app.state.service_authority
    def jwt(req):
        value=req.headers.get('Authorization','')
        if not value.startswith('Bearer '):raise HTTPException(401,'missing_user_token')
        return value[7:]
    @app.get('/internal/ma-ops/v1/assets')
    async def assets(req:Request):
        await svc().guard();await svc().identity(jwt(req),req.state.actor['user_id'])
        value=await app.state.store.list_assets(kind='service')
        value['list']=[a for a in value['list'] if a['environment'] in ('acceptance','fixture') and a['criticality'] in ('support','drill')];value['total']=len(value['list']);return value
    @app.post('/internal/ma-ops/v1/actions')
    async def propose(body:contract.Proposal,req:Request):
        raw=body.model_dump(mode='json')
        try:
            sig=verify(req.headers.get('X-MA-Envelope',''),app.state.cfg.envelope_key,'ma-envelope')
            if sig['hash']!=envelope_digest(raw) or req.state.actor['session_id']!=str(body.session_id) or req.state.actor['trace_id']!=str(body.run_id) or req.state.actor['request_hash']!=__import__('hashlib').sha256(body.user_request.encode()).hexdigest():raise ValueError()
        except (ValueError,KeyError):raise HTTPException(403,'invalid_action_signature') from None
        return await svc().propose(body,req.state.actor,jwt(req))
    @app.get('/internal/ma-ops/v1/actions/{aid}')
    async def get(aid:UUID,req:Request):return await svc().reconcile(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-ops/v1/actions/{aid}/decision')
    async def decide(aid:UUID,body:Decision,req:Request):return await svc().decide(aid,body,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-ops/v1/actions/{aid}/cancel')
    async def cancel(aid:UUID,req:Request):return await svc().cancel(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-ops/v1/claims')
    async def claim(req:Request):
        raw=await req.json()
        if not isinstance(raw,dict) or set(raw)!={'grant'}:raise HTTPException(400,'invalid_claim')
        return await svc().claim(raw['grant'],jwt(req))
