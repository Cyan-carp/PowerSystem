"""Local S06 adapter over ma_tasks/ma_approvals/ma_execution_attempts.

There is one approval authority: these are the existing stage9 tables. Stage8
never signs or consumes a business approval. A dispatched write is never retried;
only its fixed backend receipt is queried after transport uncertainty.
"""
import asyncio
import json
import time
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
from fastapi import HTTPException,Request
from .business_contract import POLICY,CAPABILITIES,Proposal,Decision,arguments,binding,digest,sign_grant,verify_grant
from .security import verify
from .store import encode

def now():return datetime.now(timezone.utc)
def unpack(row):
    if row is None:return None
    d=dict(row)
    for k in ('envelope','plan','result','evidence'):
        if isinstance(d.get(k),str):d[k]=json.loads(d[k])
    return d

class BusinessAuthority:
    protocol='ma-action/v1'
    policy=POLICY
    capabilities=CAPABILITIES
    parse_arguments=staticmethod(arguments)
    build_binding=staticmethod(binding)
    audience='ma-business-execute/v1'
    adapter_path='/internal/ma-action/v1'
    @property
    def adapter_url(self):return self.cfg.backend_url
    @property
    def grant_key(self):return self.cfg.actor_key
    def adapter_headers(self,jwt):return {'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.service_token}
    def receipt_headers(self,task,attempt,jwt):return self.adapter_headers(jwt)
    def policy_snapshot(self):return dict(profile=self.policy,r1=['alarm.ack','device.remark'],sensitive=['device.remark.read'],production=False)
    def automatic(self,capability,opinion):return capability=='device.remark.read' and opinion['decision']=='allow'
    def verify_token(self,token):return verify_grant(token,self.grant_key)
    def execution_status(self,evidence):return 'succeeded'
    def validate_evidence(self,evidence,task,cid):
        if evidence.get('execution_ref')!=str(cid) or evidence.get('action_id')!=str(task['task_id']) or evidence.get('run_id')!=str(task['run_id']):raise ValueError('invalid_execution_receipt')
    def __init__(self,app,client):self.app,self.client,self.tasks=app,client,set()
    @property
    def cfg(self):return self.app.state.cfg
    @property
    def pool(self):return self.app.state.store.pool
    async def guard(self):
        from v4_joint import selected, guard
        if selected(self.cfg):
            try:await guard(self.cfg,self.pool,'ops')
            except ValueError:raise HTTPException(403,'isolated_s09_policy_required') from None
            return
        cfg=self.cfg;db=urlsplit(cfg.database_url)
        if not getattr(cfg,'v4_actions_enabled',False):raise HTTPException(403,'business_actions_disabled')
        if db.hostname!='127.0.0.1' or db.port!=25436 or not db.path.startswith('/v4_s06_') or not db.path.endswith('_test') or cfg.backend_url!='http://127.0.0.1:28962' or cfg.security_url!='http://127.0.0.1:28964' or cfg.write_enabled or cfg.runtime_profile!='legacy':raise HTTPException(403,'isolated_s06_policy_required')
        name=await self.pool.fetchval('SELECT current_database()')
        if not name.startswith('v4_s06_') or not name.endswith('_test'):raise HTTPException(403,'isolated_s06_database_required')
    def spawn(self,coro):
        task=asyncio.create_task(coro);self.tasks.add(task);task.add_done_callback(self.tasks.discard)
    async def identity(self,jwt,owner,role=None):
        r=await self.client.get(self.cfg.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.service_token},timeout=3)
        if r.status_code!=200:raise HTTPException(403,'permission_changed')
        value=r.json();actor=value.get('data',value)
        if 'context' in actor:
            try:actor=verify(actor['context'],self.cfg.actor_key,'ma-ops')
            except ValueError:raise HTTPException(403,'permission_changed') from None
        if actor.get('user_id')!=owner or actor.get('role') not in ('operator','admin') or role and actor['role']!=role:raise HTTPException(409,'permission_changed')
        return actor
    async def snapshot(self,cap,args,jwt):
        r=await self.client.post(self.cfg.backend_url+'/internal/ma-action/v1/snapshot',json=dict(capability=cap,target_id=args.get('alarm_id',args.get('device_id'))),headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.service_token},timeout=3)
        if r.status_code!=200:raise HTTPException(409 if r.status_code in (404,409) else 503,'target_unavailable')
        return r.json()
    async def get_task(self,aid,owner,c=None):
        row=await (c or self.pool).fetchrow("SELECT * FROM ma_tasks WHERE task_id=$1 AND user_id=$2 AND envelope->>'protocol'=$3",aid,owner,self.protocol)
        if not row:raise HTTPException(404,'action_not_found')
        return unpack(row)
    async def event(self,c,task,state,reason):
        await c.execute("UPDATE ma_tasks SET status=$2,updated_at=now(),finished_at=CASE WHEN $3 THEN now() ELSE NULL END WHERE task_id=$1",task['task_id'],state,state in ('succeeded','failed','cancelled'))
        await c.execute('INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,$3,1,$4)',task['task_id'],task['status'],state,reason)
        await c.execute('UPDATE ma_approvals SET authority_revision=authority_revision+1 WHERE task_id=$1',task['task_id'])
    async def invalidate(self,c,task,code,status='invalidated'):
        await c.execute('UPDATE ma_approvals SET status=$2 WHERE task_id=$1 AND consumed_at IS NULL',task['task_id'],status)
        await c.execute("UPDATE ma_task_steps SET status='cancelled',finished_at=now() WHERE task_id=$1",task['task_id'])
        await c.execute('UPDATE ma_tasks SET result=$2::jsonb WHERE task_id=$1',task['task_id'],encode(dict(error=dict(code=code))))
        await self.event(c,task,'cancelled' if status in ('rejected','expired','cancelled') else 'failed',code)
    async def propose(self,body,actor,jwt):
        await self.guard()
        try:args=self.parse_arguments(body.capability,body.typed_args)
        except (ValueError,KeyError):raise HTTPException(400,'invalid_action_arguments') from None
        await self.identity(jwt,actor['user_id'],actor['role'])
        original=body.model_dump(mode='json');request_hash=digest(dict(owner=actor['user_id'],body=original))
        # Persist an immutable task/step before the model request. Lost responses
        # find this same task and never create another approval or model review.
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(906001)')
            old=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1',body.action_id)
            if old:
                if old['user_id']!=actor['user_id'] or old['request_hash']!=request_hash:raise HTTPException(409,'action_binding_conflict')
                return await self.projection(body.action_id,actor['user_id'],c=c)
            env=dict(protocol=self.protocol,actor=dict(user_id=actor['user_id'],role=actor['role']),intent='S06 fixed business action',**original)
            await c.execute("INSERT INTO ma_tasks(task_id,trace_id,session_id,user_id,parent_turn_id,idempotency_key,request_hash,envelope,status,deadline,execution_epoch,run_id,link_pending) VALUES($1,$1,$2,$3,$4,$1,$5,$6::jsonb,'planning',now()+interval '5 minutes',1,$7,false)",body.action_id,body.session_id,actor['user_id'],body.parent_turn_id,request_hash,encode(env),body.run_id)
            await c.execute("INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,risk_class,status,execution_epoch) VALUES($1,1,$2,$3::jsonb,$4,$5,'pending',1)",body.action_id,body.capability,encode(args),digest(args),self.capabilities[body.capability][0])
            await c.execute("INSERT INTO ma_task_events(task_id,to_status,execution_epoch,reason) VALUES($1,'planning',1,'action_proposed')",body.action_id)
        try:
            snap=await self.snapshot(body.capability,args,jwt)
            if body.capability=='device.remark' and snap['target_version']!=str(args['expected_version']):raise HTTPException(409,'target_version_changed')
            expiry=(now()+timedelta(minutes=5)).isoformat()
            bound=self.build_binding(actor['user_id'],body,args,snap['target_version'],expiry)
            plan=dict(binding=bound,plan_hash=digest(bound),actor_role=actor['role'],target_summary=snap['target_summary'],risk_class=self.capabilities[body.capability][0])
            opinion=await self.review(body.action_id,actor['user_id'],plan)
            if opinion['decision']=='deny':raise HTTPException(409,'security_denied')
            async with self.pool.acquire() as c,c.transaction():
                task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',body.action_id))
                if task['cancel_requested'] or task['status']!='planning':raise HTTPException(409,'action_cancelled')
                await c.execute('INSERT INTO ma_policy_versions(version,snapshot) VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING',self.policy,encode(self.policy_snapshot()))
                aid=uuid4()
                automatic=self.automatic(body.capability,opinion)
                plan['review_id']=opinion['review_id']
                await c.execute("INSERT INTO ma_approvals(approval_id,task_id,step_no,plan,args_hash,policy_version,status,expires_at,decided_at) VALUES($1,$2,1,$3::jsonb,$4,$5,$6,$7,$8)",aid,body.action_id,encode(plan),digest(args),self.policy,'approved' if automatic else 'pending',datetime.fromisoformat(expiry),now() if automatic else None)
                await self.event(c,task,'queued' if automatic else 'awaiting_approval','review_allow_approved_read' if automatic else 'approval_required')
            if automatic:self.spawn(self.execute(body.action_id,jwt))
        except Exception as exc:
            code=exc.detail if isinstance(exc,HTTPException) else 'security_unavailable'
            async with self.pool.acquire() as c,c.transaction():
                task=await self.get_task(body.action_id,actor['user_id'],c)
                if task['status']=='planning':
                    await c.execute('UPDATE ma_tasks SET result=$2::jsonb WHERE task_id=$1',body.action_id,encode(dict(error=dict(code=code))))
                    await self.event(c,task,'failed',code)
        return await self.projection(body.action_id,actor['user_id'])
    async def review(self,tid,owner,plan):
        from .runtime_reviews import contract
        rid=uuid4();bound=plan['binding'];args=bound['typed_args']
        raw=dict(protocol='ma-action-review/v1',review_id=str(rid),binding_hash=plan['plan_hash'],capability=bound['capability'],data_class=bound['data_class'],destination=bound['destination_id'],risk_class=plan['risk_class'],owner_id=owner,policy_version=POLICY,target_version=bound['target_version'],argument_hash=digest(args),remark_length=len(args.get('remark','')),expires_at=int(datetime.fromisoformat(bound['expires_at']).timestamp()))
        await self.pool.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,1,$2,$3,1,'v4_business',$4,$5,$6::jsonb,'pending','deepseek-flash')",rid,tid,owner,contract.digest(raw),plan['plan_hash'],encode(raw))
        try:
            r=await self.client.post(self.cfg.security_url+'/internal/ma-action-review/v1/reviews',json=raw,headers={'X-SEC-Service-Token':self.cfg.security_service_token,'X-SEC-Request':contract.sign_message(self.cfg.security_request_key,'ma-action-review-request/v1',rid,contract.digest(raw),raw['expires_at'])},timeout=32)
            if r.status_code!=200:raise ValueError()
            payload=r.json();op=payload['decision']
            contract.verify_message(r.headers.get('X-SEC-Result',''),self.cfg.security_result_key,'ma-action-review-result/v1',rid,contract.digest(op))
            if op['binding_hash']!=plan['plan_hash'] or op['decision'] not in ('allow','require_approval','deny') or op['expires_at']<=time.time():raise ValueError()
            await self.pool.execute("UPDATE ma_security_reviews SET state='completed',decision=$2::jsonb,decision_hash=$3,signature_validated=true,expires_at=$4,usage=$5::jsonb,duration_ms=$6,finished_at=now() WHERE review_id=$1",rid,encode(op),contract.digest(op),datetime.fromtimestamp(op['expires_at'],timezone.utc),encode(payload.get('usage',{})),payload.get('duration_ms'))
            return op
        except Exception:
            await self.pool.execute("UPDATE ma_security_reviews SET state='unavailable',error_code='security_unavailable',finished_at=now() WHERE review_id=$1",rid)
            raise HTTPException(503,'security_unavailable') from None
        finally:
            await self.pool.execute('INSERT INTO ma_security_review_events(review_id,state,reason) SELECT review_id,state,coalesce(error_code,state) FROM ma_security_reviews WHERE review_id=$1',rid)
    async def projection(self,aid,owner,c=None):
        db=c or self.pool;task=await self.get_task(aid,owner,db)
        a=unpack(await db.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid));attempt=unpack(await db.fetchrow('SELECT * FROM ma_execution_attempts WHERE task_id=$1',aid))
        if not a:
            return dict(action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),capability=task['envelope']['capability'],status='unknown' if task['status']=='planning' else 'blocked',authority_revision=1,observed_at=now().isoformat(),stale=False,error_code=(task['result'].get('error') or {}).get('code'),effect_summary='安全审查尚未完成' if task['status']=='planning' else '操作已阻断，未执行',target_summary='')
        plan=a['plan'];bound=plan['binding'];state=attempt['status'] if attempt else a['status']
        evidence=(attempt or {}).get('evidence') or {}
        return dict(action_id=str(aid),task_id=str(aid),run_id=str(task['run_id']),approval_id=str(a['approval_id']),plan_hash=plan['plan_hash'],expires_at=a['expires_at'].isoformat(),risk_class=plan['risk_class'],status=state,approval_status=a['status'],consumed_at=a['consumed_at'].isoformat() if a['consumed_at'] else None,execution_ref=str(attempt['call_id']) if attempt else None,authority_revision=a['authority_revision'],observed_at=now().isoformat(),stale=False,effect_summary=evidence.get('effect_summary') or ('确认该条告警（不代表故障修复）' if bound['capability']=='alarm.ack' else '修改设备备注为：'+bound['typed_args']['remark'] if bound['capability']=='device.remark' else '审查后读取本人可访问的设备备注'),target_summary=plan['target_summary'],target_version=bound['target_version'],capability=bound['capability'],typed_args=bound['typed_args'],error_code=(task['result'].get('error') or {}).get('code'),display_result=evidence.get('remark'),no_effect=evidence.get('no_effect',False))
    async def validate_plan(self,task,a,jwt):
        plan=a['plan'];bound=plan['binding']
        await self.guard();await self.identity(jwt,task['user_id'],plan['actor_role'])
        if bound['policy_version']!=self.policy or digest(bound)!=plan['plan_hash'] or digest(bound['typed_args'])!=a['args_hash']:raise HTTPException(409,'approval_binding_changed')
        if a['expires_at']<=now():raise HTTPException(409,'approval_expired')
        review=await self.pool.fetchrow('SELECT * FROM ma_security_reviews WHERE review_id=$1',UUID(plan['review_id']))
        if not review or review['state']!='completed' or not review['signature_validated'] or review['expires_at']<=now() or review['binding_hash']!=plan['plan_hash']:raise HTTPException(409,'security_review_stale')
        snap=await self.snapshot(bound['capability'],bound['typed_args'],jwt)
        if snap['target_version']!=bound['target_version']:raise HTTPException(409,'target_version_changed')
    async def decide(self,aid,body,owner,jwt):
        await self.guard();task=await self.get_task(aid,owner)
        if str(body.run_id)!=str(task['run_id']):raise HTTPException(409,'approval_binding_changed')
        a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 AND approval_id=$2',aid,body.approval_id))
        if not a or a['plan']['plan_hash']!=body.plan_hash:raise HTTPException(409,'approval_binding_changed')
        # Permission is rechecked even for replay; a valid replay returns original state.
        await self.identity(jwt,owner,a['plan']['actor_role'])
        wanted='approved' if body.decision=='approve' else 'rejected'
        error=None
        if a['status']=='pending' and body.decision=='approve':
            try:await self.validate_plan(task,a,jwt)
            except HTTPException as e:error=e
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            a=unpack(await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 FOR UPDATE',aid))
            if a['status']==wanted:return await self.projection(aid,owner,c)
            if a['status']!='pending':raise HTTPException(409,'approval_decision_conflict')
            if task['cancel_requested']:raise HTTPException(409,'action_cancelled')
            if a['expires_at']<=now():error=HTTPException(409,'approval_expired')
            if error:await self.invalidate(c,task,error.detail,'expired' if error.detail=='approval_expired' else 'invalidated')
            elif body.decision=='reject':
                await c.execute('UPDATE ma_approvals SET decided_at=now() WHERE task_id=$1',aid)
                await self.invalidate(c,task,'approval_rejected','rejected')
            else:
                await c.execute("UPDATE ma_approvals SET status='approved',decided_at=now() WHERE task_id=$1",aid)
                await self.event(c,task,'queued','approval_approved')
        if error:raise error
        if body.decision=='approve':self.spawn(self.execute(aid,jwt))
        return await self.projection(aid,owner)
    async def execute(self,aid,jwt):
        task=unpack(await self.pool.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1',aid));owner=task['user_id']
        a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid));cid=uuid4()
        try:await self.validate_plan(task,a,jwt)
        except HTTPException as e:
            async with self.pool.acquire() as c,c.transaction():
                task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
                if task['status']=='queued':await self.invalidate(c,task,e.detail)
            return
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            a=unpack(await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 FOR UPDATE',aid))
            if a['consumed_at'] or a['status']!='approved' or task['status']!='queued':return
            if task['cancel_requested']:
                await self.invalidate(c,task,'action_cancelled','cancelled');return
            if a['expires_at']<=now():
                await self.invalidate(c,task,'approval_expired','expired');return
            await c.execute('SELECT pg_advisory_xact_lock($1)',int(a['plan']['plan_hash'][:15],16))
            await c.execute("INSERT INTO ma_tool_calls(call_id,task_id,step_no,trace_id,execution_epoch,args_hash,status) VALUES($1,$2,1,$2,1,$3,'running')",cid,aid,a['args_hash'])
            await c.execute("INSERT INTO ma_execution_attempts(attempt_id,task_id,step_no,approval_id,call_id,execution_epoch,status) VALUES($1,$2,1,$3,$1,1,'started')",cid,aid,a['approval_id'])
            await c.execute('UPDATE ma_approvals SET consumed_at=now() WHERE task_id=$1',aid)
            await c.execute("UPDATE ma_task_steps SET status='running' WHERE task_id=$1",aid)
            await self.event(c,task,'executing','approval_consumed')
        bound=a['plan']['binding'];grant=dict(aud=self.audience,iat=int(time.time()),exp=min(int(a['expires_at'].timestamp()),int(time.time())+60),owner_id=owner,run_id=str(task['run_id']),action_id=str(aid),execution_ref=str(cid),capability=bound['capability'],typed_args=bound['typed_args'],target_version=bound['target_version'],plan_hash=a['plan']['plan_hash'],policy_version=self.policy)
        token=sign_grant(grant,self.grant_key)
        try:
            r=await self.client.post(self.adapter_url+self.adapter_path+'/execute',json={'grant':token},headers=self.adapter_headers(jwt),timeout=8)
            if r.status_code==200:
                evidence=r.json()
                self.validate_evidence(evidence,task,cid)
                await self.finish(aid,self.execution_status(evidence),evidence);return
            if r.status_code!=200:
                # An unclaimed adapter is fenced while holding the same task lock
                # used by claims, so a delayed request cannot acquire it later.
                if await self.fail_unclaimed(aid,r.status_code):return
                value=r.json()
                if value.get('no_effect') is True and value.get('execution_ref')==str(cid) and value.get('action_id')==str(aid):
                    await self.finish(aid,'failed',dict(no_effect=True,effect_summary='业务事务拒绝并回滚，未修改业务数据',http_status=r.status_code));return
        except Exception:pass
        await self.pool.execute("UPDATE ma_execution_attempts SET status='execution_uncertain' WHERE task_id=$1 AND status='started'",aid)
        await self.pool.execute('UPDATE ma_approvals SET authority_revision=authority_revision+1 WHERE task_id=$1',aid)
    async def fail_unclaimed(self,aid,status):
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            call=await c.fetchrow('SELECT * FROM ma_tool_calls WHERE task_id=$1 FOR UPDATE',aid)
            if not call or call['adapter_claimed_at'] is not None:return False
            evidence=dict(no_effect=True,effect_summary='适配器尚未领取且执行上下文已封闭，未修改业务数据',http_status=status)
            await c.execute("UPDATE ma_execution_attempts SET status='failed',evidence=$2::jsonb,finished_at=now() WHERE task_id=$1",aid,encode(evidence))
            await c.execute("UPDATE ma_tool_calls SET status='failed',finished_at=now() WHERE task_id=$1",aid)
            await c.execute("UPDATE ma_task_steps SET status='failed',finished_at=now() WHERE task_id=$1",aid)
            await self.event(c,task,'failed','unclaimed_adapter_fenced')
            return True
    async def finish(self,aid,status,evidence):
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            await c.execute('UPDATE ma_execution_attempts SET status=$2,evidence=$3::jsonb,finished_at=now() WHERE task_id=$1',aid,status,encode(evidence))
            await c.execute('UPDATE ma_tool_calls SET status=$2,data=$3::jsonb,finished_at=now() WHERE task_id=$1',aid,status,encode(dict(no_effect=evidence.get('no_effect',False))))
            await c.execute('UPDATE ma_task_steps SET status=$2,finished_at=now() WHERE task_id=$1',aid,status)
            await self.event(c,task,status,'business_result_recorded')
    async def reconcile(self,aid,owner,jwt):
        task=await self.get_task(aid,owner);attempt=unpack(await self.pool.fetchrow('SELECT * FROM ma_execution_attempts WHERE task_id=$1',aid))
        if not attempt:
            a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid))
            if a and a['status'] in ('pending','approved') and not a['consumed_at'] and a['expires_at']<=now():
                async with self.pool.acquire() as c,c.transaction():
                    task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
                    a=await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 FOR UPDATE',aid)
                    if a['status'] in ('pending','approved') and not a['consumed_at'] and a['expires_at']<=now():await self.invalidate(c,task,'approval_expired','expired')
        elif attempt['status'] in ('started','execution_uncertain'):
            # Read a receipt. A 404 is not proof that an in-flight transaction failed.
            try:
                r=await self.client.get(self.adapter_url+self.adapter_path+'/receipts/'+str(attempt['call_id']),headers=self.receipt_headers(task,attempt,jwt),timeout=3)
                if r.status_code==200:
                    v=r.json()
                    self.validate_evidence(v,task,attempt['call_id'])
                    await self.finish(aid,self.execution_status(v),v)
            except Exception:pass
            if (now()-attempt['started_at']).total_seconds()>120:
                await self.pool.execute("UPDATE ma_tasks SET status='failed',result=result||'{\"requires_manual_reconciliation\":true}'::jsonb WHERE task_id=$1 AND status='executing'",aid)
        return await self.projection(aid,owner)
    async def cancel(self,aid,owner,jwt):
        await self.get_task(aid,owner)
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            if task['status'] not in ('succeeded','failed','cancelled'):
                await c.execute('UPDATE ma_tasks SET cancel_requested=true WHERE task_id=$1',aid)
                a=await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 FOR UPDATE',aid)
                if a and not a['consumed_at']:await self.invalidate(c,task,'action_cancelled','cancelled')
                elif not a:await self.event(c,task,'cancelled','action_cancelled')
        return await self.reconcile(aid,owner,jwt)
    async def claim(self,token,jwt):
        await self.guard()
        try:g=self.verify_token(token)
        except ValueError:raise HTTPException(403,'invalid_grant') from None
        aid=UUID(g['action_id']);task=await self.get_task(aid,g['owner_id']);a=unpack(await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',aid))
        await self.identity(jwt,g['owner_id'],a['plan']['actor_role'])
        bound=a['plan']['binding']
        if g['run_id']!=str(task['run_id']) or g['capability']!=bound['capability'] or g['typed_args']!=bound['typed_args'] or g['target_version']!=bound['target_version'] or g['plan_hash']!=a['plan']['plan_hash'] or g['policy_version']!=self.policy:raise HTTPException(403,'grant_binding_changed')
        async with self.pool.acquire() as c,c.transaction():
            task=unpack(await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',aid))
            a=unpack(await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 FOR UPDATE',aid))
            call=await c.fetchrow('SELECT * FROM ma_tool_calls WHERE call_id=$1 FOR UPDATE',UUID(g['execution_ref']))
            if not call or call['task_id']!=aid or call['adapter_claimed_at'] or a['status']!='approved' or not a['consumed_at'] or a['expires_at']<=now() or task['cancel_requested'] or task['status']!='executing':raise HTTPException(409,'grant_consumed_or_cancelled')
            await c.execute('UPDATE ma_tool_calls SET adapter_claimed_at=now() WHERE call_id=$1',call['call_id'])
        return dict(authorized=True)

def routes(app):
    def svc():return app.state.business_authority
    def jwt(req):
        token=req.headers.get('Authorization','')
        if not token.startswith('Bearer '):raise HTTPException(401,'missing_user_token')
        return token[7:]
    @app.post('/internal/ma-action/v1/actions')
    async def propose(body:Proposal,req:Request):
        raw=body.model_dump(mode='json')
        try:
            sig=verify(req.headers.get('X-MA-Envelope',''),app.state.cfg.envelope_key,'ma-envelope')
            if sig['hash']!=__import__('ops_agent.security',fromlist=['digest']).digest(raw) or req.state.actor['session_id']!=str(body.session_id) or req.state.actor['trace_id']!=str(body.run_id) or req.state.actor['request_hash']!=__import__('hashlib').sha256(body.user_request.encode()).hexdigest():raise ValueError()
        except (ValueError,KeyError):raise HTTPException(403,'invalid_action_signature') from None
        return await svc().propose(body,req.state.actor,jwt(req))
    @app.get('/internal/ma-action/v1/actions/{aid}')
    async def get(aid:UUID,req:Request):return await svc().reconcile(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-action/v1/actions/{aid}/decision')
    async def decide(aid:UUID,body:Decision,req:Request):return await svc().decide(aid,body,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-action/v1/actions/{aid}/cancel')
    async def cancel(aid:UUID,req:Request):return await svc().cancel(aid,req.state.actor['user_id'],jwt(req))
    @app.post('/internal/ma-action/v1/claims')
    async def claim(req:Request):
        raw=await req.json()
        if not isinstance(raw,dict) or set(raw)!={'grant'} or not isinstance(raw['grant'],str):raise HTTPException(400,'invalid_claim')
        return await svc().claim(raw['grant'],jwt(req))
