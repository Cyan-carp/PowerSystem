"""Schedule configuration reuses the existing action/approval/execution ledger."""
import json,time
from datetime import datetime,timezone
from urllib.parse import urlsplit
from uuid import UUID,uuid4
from fastapi import HTTPException,Request
from .business_actions import BusinessAuthority,unpack
from .business_contract import Decision
from . import schedule_contract as contract
from .security import verify,digest as envelope_digest
from .store import encode

class ScheduleAuthority(BusinessAuthority):
    protocol=contract.PROTOCOL;policy=contract.POLICY;capabilities=contract.CAPABILITIES
    parse_arguments=staticmethod(contract.arguments);build_binding=staticmethod(contract.binding)
    audience='ma-schedule-execute/v1';adapter_path='/internal/ma-schedule/v1'
    @property
    def adapter_url(self):return self.cfg.supervisor_url
    def adapter_headers(self,jwt):return {'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.knowledge_token}
    def automatic(self,capability,opinion):return False
    def verify_token(self,token):return contract.verify_grant(token,self.grant_key)
    def execution_status(self,evidence):return evidence.get('status','succeeded')
    def policy_snapshot(self):return dict(profile=self.policy,r1=list(self.capabilities),trigger_r0=['daily_report','inspection'],production=False)
    async def guard(self):
        from v4_joint import selected, guard
        if selected(self.cfg):
            try:await guard(self.cfg,self.pool,'ops')
            except ValueError:raise HTTPException(403,'isolated_s09_policy_required') from None
            return
        c=self.cfg;db=urlsplit(c.database_url)
        if not getattr(c,'v4_schedules_enabled',False):raise HTTPException(403,'schedules_disabled')
        if db.hostname!='127.0.0.1' or db.port!=25438 or not db.path.startswith('/v4_s08_') or not db.path.endswith('_test') or c.backend_url!='http://127.0.0.1:28982' or c.supervisor_url!='http://127.0.0.1:28981' or c.security_url!='http://127.0.0.1:28984' or c.runtime_profile!='legacy' or c.write_enabled or c.v4_actions_enabled or c.v4_ops_enabled:raise HTTPException(403,'isolated_s08_policy_required')
        if not (await self.pool.fetchval('SELECT current_database()')).startswith('v4_s08_'):raise HTTPException(403,'isolated_s08_database_required')
    async def snapshot(self,cap,args,jwt):
        r=await self.client.post(self.adapter_url+self.adapter_path+'/snapshot',json=dict(capability=cap,typed_args=args),headers=self.adapter_headers(jwt),timeout=3)
        if r.status_code!=200:raise HTTPException(409,'schedule_target_changed')
        return r.json()
    async def review(self,tid,owner,plan):
        from .runtime_reviews import contract as rc
        rid=uuid4();b=plan['binding']
        raw=dict(protocol='ma-schedule-review/v1',review_id=str(rid),binding_hash=plan['plan_hash'],capability=b['capability'],data_class='internal',destination=b['destination_id'],risk_class='R1',owner_id=owner,policy_version=self.policy,target_version=b['target_version'],argument_hash=contract.digest(b['typed_args']),expires_at=int(datetime.fromisoformat(b['expires_at']).timestamp()))
        await self.pool.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,1,$2,$3,1,'v4_schedule',$4,$5,$6::jsonb,'pending','deepseek-flash')",rid,tid,owner,rc.digest(raw),plan['plan_hash'],encode(raw))
        try:
            r=await self.client.post(self.cfg.security_url+'/internal/ma-schedule-review/v1/reviews',json=raw,headers={'X-SEC-Service-Token':self.cfg.security_service_token,'X-SEC-Request':rc.sign_message(self.cfg.security_request_key,'ma-schedule-review-request/v1',rid,rc.digest(raw),raw['expires_at'])},timeout=32)
            if r.status_code!=200:raise ValueError()
            payload=r.json();op=payload['decision']
            rc.verify_message(r.headers.get('X-SEC-Result',''),self.cfg.security_result_key,'ma-schedule-review-result/v1',rid,rc.digest(op))
            if op['protocol']!='ma-schedule-review/v1' or op['review_id']!=str(rid) or op['binding_hash']!=plan['plan_hash'] or op['decision'] not in ('allow','require_approval','deny') or not time.time()<op['expires_at']<=raw['expires_at']:raise ValueError()
            await self.pool.execute("UPDATE ma_security_reviews SET state='completed',decision=$2::jsonb,decision_hash=$3,signature_validated=true,expires_at=$4,usage=$5::jsonb,duration_ms=$6,finished_at=now() WHERE review_id=$1",rid,encode(op),rc.digest(op),datetime.fromtimestamp(op['expires_at'],timezone.utc),encode(payload.get('usage',{})),payload.get('duration_ms'))
            return op
        except Exception:
            await self.pool.execute("UPDATE ma_security_reviews SET state='unavailable',error_code='security_unavailable',finished_at=now() WHERE review_id=$1",rid)
            raise HTTPException(503,'security_unavailable') from None
        finally:await self.pool.execute('INSERT INTO ma_security_review_events(review_id,state,reason) SELECT review_id,state,coalesce(error_code,state) FROM ma_security_reviews WHERE review_id=$1',rid)
    async def validate_plan(self,task,a,jwt):
        await super().validate_plan(task,a,jwt)
        b=a['plan']['binding'];env=task['envelope']
        expected=contract.binding(task['user_id'],contract.Proposal.model_validate({k:env[k] for k in contract.Proposal.model_fields}),contract.arguments(env['capability'],env['typed_args']),b['target_version'],b['expires_at'])
        if b!=expected or a['policy_version']!=self.policy or a['plan']['risk_class']!='R1':raise HTTPException(409,'approval_binding_changed')
    async def claim(self,token,jwt):
        try:g=self.verify_token(token)
        except ValueError:raise HTTPException(403,'invalid_grant') from None
        task=await self.get_task(UUID(g['action_id']),g['owner_id']);a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',task['task_id']))
        if not a:raise HTTPException(409,'approval_missing')
        await self.validate_plan(task,a,jwt)
        value=await super().claim(token,jwt)
        return {**value,'actor_role':a['plan']['actor_role']}
    async def projection(self,aid,owner,c=None):
        db=c or self.pool;task=await self.get_task(aid,owner,db);a=unpack(await db.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid));attempt=unpack(await db.fetchrow('SELECT * FROM ma_execution_attempts WHERE task_id=$1',aid))
        cap=task['envelope']['capability']
        if not a:return dict(action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),capability=cap,status='unknown' if task['status']=='planning' else 'blocked',risk_class='R1',authority_revision=1,observed_at=datetime.now(timezone.utc).isoformat(),stale=False,error_code=(task['result'].get('error') or {}).get('code'),effect_summary='调度配置尚未生效',target_summary='')
        p=a['plan'];b=p['binding'];e=(attempt or {}).get('evidence') or {}
        return dict(protocol=self.protocol,action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),capability=cap,approval_id=str(a['approval_id']),plan_hash=p['plan_hash'],expires_at=a['expires_at'].isoformat(),risk_class='R1',status=attempt['status'] if attempt else a['status'],approval_status=a['status'],consumed_at=str(a['consumed_at']) if a['consumed_at'] else None,execution_ref=str(attempt['call_id']) if attempt else None,authority_revision=a['authority_revision'],observed_at=datetime.now(timezone.utc).isoformat(),stale=False,effect_summary=e.get('effect_summary','启用有限期本人只读调度；不会批准或执行业务/运维动作'),target_summary=p['target_summary'],target_version=b['target_version'],typed_args=b['typed_args'],parameters_summary=json.dumps(b['typed_args'],ensure_ascii=False,sort_keys=True),policy_version=self.policy,destination_id='platform_client',recovery_limit='暂停/取消会撤销grant；恢复及改配置重新审查批准，不自动续期。已交付产物仍保留。',no_effect=e.get('no_effect',False),error_code=(task['result'].get('error') or {}).get('code'))

def routes(app):
    def svc():return app.state.schedule_authority
    def jwt(req):
        v=req.headers.get('Authorization','')
        if not v.startswith('Bearer '):raise HTTPException(401,'missing_user_token')
        return v[7:]
    @app.post('/internal/ma-schedule/v1/actions')
    async def propose(body:contract.Proposal,req:Request):
        try:
            sig=verify(req.headers.get('X-MA-Envelope',''),app.state.cfg.envelope_key,'ma-envelope')
            if sig['hash']!=envelope_digest(body.model_dump(mode='json')) or req.state.actor['session_id']!=str(body.session_id) or req.state.actor['trace_id']!=str(body.run_id) or req.state.actor['request_hash']!=__import__('hashlib').sha256(body.user_request.encode()).hexdigest():raise ValueError()
        except (ValueError,KeyError):raise HTTPException(403,'invalid_action_signature') from None
        return await svc().propose(body,req.state.actor,jwt(req))
    @app.get('/internal/ma-schedule/v1/actions/{aid}')
    async def get(aid:UUID,req:Request):return await svc().reconcile(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-schedule/v1/actions/{aid}/decision')
    async def decide(aid:UUID,body:Decision,req:Request):return await svc().decide(aid,body,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-schedule/v1/actions/{aid}/cancel')
    async def cancel(aid:UUID,req:Request):return await svc().cancel(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-schedule/v1/claims')
    async def claim(req:Request):
        raw=await req.json()
        if not isinstance(raw,dict) or set(raw)!={'grant'}:raise HTTPException(400,'invalid_claim')
        return await svc().claim(raw['grant'],jwt(req))
