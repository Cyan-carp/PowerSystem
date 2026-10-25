"""Necessary S07 HTTP/DB acceptance only. Never runs without --run."""
import asyncio,json,sys,time,hashlib
from pathlib import Path
from uuid import uuid4,UUID
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'artifacts/v4-assistant/s07-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
import httpx,asyncpg
from ops_agent.security import claims,sign
from rag_agent.ops import envelope_signature
from ops_agent.business_contract import canonical,digest,sign_grant

BASE='http://127.0.0.1:28972';DSN='postgresql://s07_test@127.0.0.1:25437/v4_s07_20261024_r1_test'
ACTOR='s07-local-synthetic-actor-key-000000000';TOKEN='s07-local-synthetic-ops-token-000000000';ENV='s07-local-synthetic-envelope-key-000000'
TERMINAL=('succeeded','failed','partial','cancelled')

class Check:
    def __init__(self,c,db):self.c,self.db,self.tokens,self.runs,self.checks=c,db,{},[],[]
    async def login(self,owner=70001):
        r=await self.c.post(BASE+'/api/v1/auth/login',json=dict(username='s07-user-'+str(owner),password='S07-local-only-2026!'));r.raise_for_status();self.tokens[owner]=r.json()['data']['token']
    def h(self,owner=70001):return {'Authorization':'Bearer '+self.tokens[owner]}
    async def mode(self,mode):r=await self.c.post('http://127.0.0.1:28974/drill/mode',json={'mode':mode});r.raise_for_status()
    async def fault(self,mode):r=await self.c.post('http://127.0.0.1:28975/drill/fault',json={'mode':mode});r.raise_for_status()
    async def create(self,message,owner=70001,session=None,key=None):
        body=dict(message=message)
        if session:body['session_id']=session
        r=await self.c.post(BASE+'/api/v1/agent/runs',json=body,headers={**self.h(owner),'Idempotency-Key':str(key or uuid4())});r.raise_for_status();value=r.json();value['owner']=owner;self.runs.append(value);return value
    async def get(self,run):
        r=await self.c.get(BASE+'/api/v1/agent/runs/'+run['run_id'],headers=self.h(run['owner']));r.raise_for_status();return r.json()
    async def wait(self,run,states=TERMINAL,seconds=25):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            value=await self.get(run)
            if value['status'] in states:return value
            await asyncio.sleep(.12)
        raise AssertionError('run wait timeout: '+json.dumps(value,ensure_ascii=False))
    async def pending(self,message,owner=70001,session=None):
        run=await self.create(message,owner,session);value=await self.wait(run,('awaiting_approval',*TERMINAL));assert value['status']=='awaiting_approval',value;return run,value['actions'][0]
    async def decide(self,run,a,decision='approve',owner=None,**changes):
        return await self.c.post(BASE+'/api/v1/agent/runs/'+run['run_id']+'/actions/'+a['action_id']+'/decision',headers=self.h(owner or run['owner']),json=dict(approval_id=a['approval_id'],plan_hash=a['plan_hash'],decision=decision,**changes))
    async def counts(self,aid):
        import sqlite3
        q=sqlite3.connect(OUT/'adapter-runtime/receipts.sqlite3')
        receipts=q.execute('SELECT count(*) FROM receipts WHERE action_id=?',(aid,)).fetchone()[0];q.close()
        value=dict(await self.db.fetchrow("SELECT (SELECT count(*) FROM ma_approvals WHERE task_id=$1) approvals,(SELECT count(*) FROM ma_execution_attempts WHERE task_id=$1) attempts",UUID(aid)))
        return dict(**value,receipts=receipts)
    async def health(self):return (await self.c.get('http://127.0.0.1:28976/health')).json()
    async def record(self,name,operation):
        selected=next((sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='--cases'),None)
        if selected and not __import__('re').search(selected,name):return
        before=time.monotonic()
        try:
            result=await operation();self.checks.append(dict(case=name,passed=True,duration_ms=int((time.monotonic()-before)*1000),evidence=result));print('PASS',name,flush=True)
        except Exception as e:self.checks.append(dict(case=name,passed=False,error_type=type(e).__name__,error=str(e)[:3500]));print('FAIL',name,str(e)[:300],flush=True)
        finally:
            await self.fault('none');await self.mode('allow')
            await self.db.execute("UPDATE users SET role=CASE WHEN id IN (70001,70003) THEN 'admin' ELSE 'operator' END WHERE id IN (70001,70002,70003,70004)")
            self.save()
    def save(self):
        payload=dict(phase='V4-S07',environment='dedicated PG25437; actual JWT/HTTP/runs/ma authority/registered subprocess adapter',candidate_mode='synthetic decision fixture except separately labelled official batches',checks=self.checks,runs=self.runs,passed=all(c['passed'] for c in self.checks),at=datetime.now(timezone.utc).isoformat())
        self.path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf8')

async def main():
    if '--run' not in sys.argv:raise SystemExit('--run required')
    db=await asyncpg.connect(DSN)
    async with httpx.AsyncClient(timeout=45,trust_env=False) as client:
        t=Check(client,db);t.path=OUT/('technical-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
        for user in (70001,70002,70003,70004):await t.login(user)
        async def fast_read():
            await t.mode('unavailable');r=await t.create('诊断 s07-demo');s=await t.wait(r)
            assert s['status']=='succeeded',s
            row=await db.fetchrow("SELECT model,state FROM ma_security_reviews WHERE task_id=$1",UUID(s['actions'][0]['action_id']))
            assert row['model']=='deterministic_catalog' and row['state']=='completed'
            return dict(run=r,ordinary_read_audited=True,no_security_model=True)
        await t.record('Ordinary registered diagnosis survives reviewer outage',fast_read)
        async def unavailable():
            await t.mode('unavailable');values=[]
            for message in ('日志 s07-demo','重启 s07-demo'):
                r=await t.create(message);s=await t.wait(r);assert s['status']=='failed',s
                a=s['actions'][0];assert a['error_code']=='security_unavailable';assert (await t.counts(a['action_id']))==dict(approvals=0,attempts=0,receipts=0);values.append(r)
            return values
        await t.record('Required review unavailable blocks sensitive read and R2',unavailable)
        async def deny():
            await t.mode('deny');r=await t.create('重启 s07-demo');s=await t.wait(r);assert s['status']=='failed';assert s['actions'][0]['error_code']=='security_denied';return dict(run=r,counts=await t.counts(s['actions'][0]['action_id']))
        await t.record('Independent deny prevents restart',deny)
        async def once():
            before=await t.health();r,a=await t.pending('重启 s07-demo');assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=0,receipts=0)
            responses=await asyncio.gather(t.decide(r,a),t.decide(r,a));assert all(v.status_code==200 for v in responses),[v.text for v in responses]
            s=await t.wait(r);after=await t.health();assert s['status']=='succeeded',s;assert before['pid']!=after['pid'] and after['generation']==before['generation']+1 and after['status']=='healthy'
            assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=1,receipts=1)
            assert (await t.decide(r,a)).status_code==200;assert (await t.decide(r,a,'reject')).status_code==409
            return dict(run=r,before=before,after=after,counts=await t.counts(a['action_id']),recovery_limit=s['actions'][0]['recovery_limit'])
        await t.record('Admin explicit approval concurrent/replayed executes real process exactly once',once)
        async def reject():
            before=await t.health();r,a=await t.pending('重启 s07-demo');assert (await t.decide(r,a,'reject')).status_code==200;s=await t.wait(r)
            assert s['status']=='cancelled';assert (await t.health())['generation']==before['generation'];assert (await t.counts(a['action_id']))['attempts']==0
            return dict(run=r,no_effect=True)
        await t.record('Reject yields zero restart',reject)
        async def bindings():
            r,a=await t.pending('重启 s07-demo')
            for user in (70002,70003):
                assert (await client.get(BASE+'/api/v1/agent/runs/'+r['run_id'],headers=t.h(user))).status_code==404
                assert (await t.decide(r,a,owner=user)).status_code==404
            assert (await t.decide(r,{**a,'plan_hash':'0'*64})).status_code==409
            assert (await t.decide(r,{**a,'approval_id':str(uuid4())})).status_code==409
            assert (await t.decide(r,a,typed_args={'asset_id':'other'})).status_code==400
            await t.decide(r,a,'reject');return dict(run=r,cross_user_admin_denied=True,unknown_decision_parameters_denied=True)
        await t.record('Owner action hash approval binding and argument injection denied',bindings)
        async def nonadmin():
            before=await t.health();r=await t.create('重启 s07-demo',70002);s=await t.wait(r);assert s['status']=='failed',s;assert (await t.health())['generation']==before['generation']
            return dict(run=r,zero_restart=True)
        await t.record('Operator cannot propose R2 or self-approve',nonadmin)
        async def expire():
            r,a=await t.pending('重启 s07-demo');await db.execute("UPDATE ma_approvals SET expires_at=now()-interval '1 second' WHERE task_id=$1",UUID(a['action_id']))
            response=await t.decide(r,a);assert response.status_code==409;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,http=409)
        await t.record('Expired approval cannot execute',expire)
        async def expiry_race():
            r,a=await t.pending('重启 s07-demo');await client.post('http://127.0.0.1:28973/drill/expiry-race');response=await t.decide(r,a);assert response.status_code==200
            s=await t.wait(r);assert s['status']=='cancelled' and s['actions'][0]['status']=='expired',s;assert (await t.counts(a['action_id']))['attempts']==0
            return dict(run=r,expired_between_validation_and_lock=True,no_stuck_approved_action=True)
        await t.record('Expiry race between validation and consumption closes without execution',expiry_race)
        async def params():
            r,a=await t.pending('重启 s07-demo');await db.execute("UPDATE ma_approvals SET plan=jsonb_set(plan,'{binding,typed_args,asset_id}','\"other\"') WHERE task_id=$1",UUID(a['action_id']))
            assert (await t.decide(r,a)).status_code==409;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,parameter_drift_rejected=True)
        await t.record('Changed immutable parameter material invalidates approval',params)
        async def destination():
            r,a=await t.pending('重启 s07-demo');await db.execute("UPDATE ma_approvals SET plan=jsonb_set(plan,'{binding,destination_id}','\"registered_model\"') WHERE task_id=$1",UUID(a['action_id']))
            assert (await t.decide(r,a)).status_code==409;await t.wait(r);return dict(run=r,destination_drift_rejected=True)
        await t.record('Destination drift cannot reuse approval',destination)
        async def target():
            r1,a1=await t.pending('重启 s07-demo');r2,a2=await t.pending('重启 s07-demo',70003)
            assert (await t.decide(r1,a1)).status_code==200;assert (await t.wait(r1))['status']=='succeeded'
            reply=await t.decide(r2,a2);assert reply.status_code==409,reply.text;assert (await t.wait(r2))['status']=='failed';assert (await t.counts(a2['action_id']))['attempts']==0
            return dict(runs=[r1,r2],actual_generation_drift=True)
        await t.record('Actual target process generation drift invalidates prior approval',target)
        async def target_race():
            r1,a1=await t.pending('重启 s07-demo');r2,a2=await t.pending('重启 s07-demo',70003);before=await t.health()
            await asyncio.gather(t.decide(r1,a1),t.decide(r2,a2));s1,s2=await asyncio.gather(t.wait(r1),t.wait(r2))
            assert sorted([s1['status'],s2['status']])==['failed','succeeded'],[s1,s2]
            assert (await t.health())['generation']==before['generation']+1
            counts=[await t.counts(a1['action_id']),await t.counts(a2['action_id'])];assert sum(v['receipts'] for v in counts)==1
            return dict(runs=[r1,r2],counts=counts,actual_one_generation_increment=True)
        await t.record('Concurrent different approvals on one target version serialize to one restart',target_race)
        async def policy_drift():
            r,a=await t.pending('重启 s07-demo');await db.execute("UPDATE ma_approvals SET plan=jsonb_set(plan,'{binding,policy_version}','\"v4-s06-local/v1\"') WHERE task_id=$1",UUID(a['action_id']))
            assert (await t.decide(r,a)).status_code==409;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,s06_policy_cannot_authorize_r2=True)
        await t.record('S06 policy drift cannot authorize S07 R2',policy_drift)
        async def revoke():
            r,a=await t.pending('重启 s07-demo',70003);await db.execute("UPDATE users SET role='operator' WHERE id=70003")
            response=await t.decide(r,a);assert response.status_code in (401,403,409),response.text
            assert (await t.counts(a['action_id']))['attempts']==0
            await db.execute("UPDATE users SET role='admin' WHERE id=70003");await t.decide(r,a,'reject');return dict(run=r,http=response.status_code,actual_role_recheck=True)
        await t.record('Role removed before approval rejects stale authorization',revoke)
        async def revoke_dispatch():
            await t.fault('pause');r,a=await t.pending('重启 s07-demo',70003);before=await t.health();assert (await t.decide(r,a)).status_code==200
            for _ in range(80):
                if (await t.counts(a['action_id']))['attempts']==1:break
                await asyncio.sleep(.05)
            await asyncio.sleep(.15);await db.execute("UPDATE users SET role='operator' WHERE id=70003");await t.fault('none');await asyncio.sleep(.3)
            await db.execute("UPDATE users SET role='admin' WHERE id=70003");s=await t.wait(r);assert s['status']=='failed',s;assert (await t.health())['generation']==before['generation'];assert (await t.counts(a['action_id']))['receipts']==0
            return dict(run=r,claim_rechecks_role=True,no_effect=True)
        await t.record('Role removed after approval before adapter claim prevents mutation',revoke_dispatch)
        async def failed():
            await t.fault('fail-before');r,a=await t.pending('重启 s07-demo');before=await t.health();assert (await t.decide(r,a)).status_code==200;s=await t.wait(r)
            assert s['status']=='failed' and s['actions'][0]['no_effect'];assert (await t.health())['generation']==before['generation'];return dict(run=r,counts=await t.counts(a['action_id']))
        await t.record('Known pre-claim failure is fenced and has no side effect',failed)
        async def health_fail():
            await t.fault('health-fail');r,a=await t.pending('重启 s07-demo');before=await t.health();assert (await t.decide(r,a)).status_code==200;s=await t.wait(r);after=await t.health()
            assert s['status']=='partial' and s['actions'][0]['status']=='failed' and s['actions'][0]['action_confirmed'],s
            assert after['generation']==before['generation']+1 and after['status']=='unhealthy';assert '不自动' in s['actions'][0]['recovery_limit'];return dict(run=r,before=before,after=after,partial_effect_retained=True)
        await t.record('Real restart with unhealthy process stays partial and gives recovery boundary',health_fail)
        async def sensitive():
            p=OUT/'adapter-runtime/service/events.jsonl'
            with p.open('a',encoding='utf8') as f:f.write(json.dumps(dict(event='service_started',ts=time.time(),message='S07-SYNTHETIC-SECRET-DO-NOT-SEND',authorization='synthetic-private-field'))+'\n')
            r=await t.create('日志 s07-demo');s=await t.wait(r);assert s['status']=='succeeded',s;body=s['actions'][0]['display_result'];assert 'S07-SYNTHETIC-SECRET' not in body and 'authorization' not in body
            value=json.loads(body);assert value['logs']['complete'] is False
            steps=await db.fetch("SELECT output,result_state FROM assistant_run_steps WHERE run_id=$1 AND kind='model'",UUID(r['run_id']));assert all('operational-events-v1' not in str(x) and 'S07-SYNTHETIC-SECRET' not in str(x) for x in steps)
            return dict(run=r,filtered_content_platform_only=True,no_raw_or_filtered_logs_to_model=True)
        await t.record('Sensitive read reviews before bounded log filter and never sends contents to model',sensitive)
        async def evidence():
            r=await t.create('证据 s07-demo');s=await t.wait(r);assert s['status']=='succeeded' and len(s['artifacts'])==1,s
            aid=s['artifacts'][0]['artifact_id'];path=BASE+'/api/v1/agent/artifacts/'+aid+'/download'
            for user in (70002,70003):assert (await client.get(path,headers=t.h(user))).status_code==404
            response=await client.get(path,headers=t.h());assert response.status_code==200;v=response.json();assert v['run_id']==r['run_id'] and v['observation']['logs']['complete'] is False
            assert hashlib.sha256(response.content).hexdigest()==s['artifacts'][0]['sha256'];assert 'S07-SYNTHETIC-SECRET' not in response.text
            return dict(run=r,artifact_id=aid,owner_only=True,sha256=s['artifacts'][0]['sha256'],actual_json_download=True)
        await t.record('Reviewed evidence package reuses authenticated S05 file delivery',evidence)
        async def unknown():
            await t.fault('drop-result');r,a=await t.pending('重启 s07-demo');before=await t.health();assert (await t.decide(r,a)).status_code==200;await t.wait(r,('reconciling',*TERMINAL))
            for _ in range(80):
                if (await t.counts(a['action_id']))['receipts']==1:break
                await asyncio.sleep(.05)
            response=await client.post(BASE+'/api/v1/agent/runs/'+r['run_id']+'/cancel',headers=t.h());assert response.status_code in (200,202) and response.json()['status'] not in ('cancelled','succeeded')
            await t.fault('none');s=await t.wait(r);after=await t.health();assert s['status']=='partial' and s['actions'][0]['status']=='succeeded',s
            assert after['generation']==before['generation']+1;assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=1,receipts=1);return dict(run=r,cancel_does_not_rollback=True,receipt_only=True,exactly_one_real_restart=True)
        await t.record('Lost execution response reconciles by durable receipt without resend or false cancellation',unknown)
        async def unresolved():
            await t.fault('unresolved');r,a=await t.pending('重启 s07-demo');assert (await t.decide(r,a)).status_code==200;await t.wait(r,('reconciling',*TERMINAL))
            await db.execute("UPDATE assistant_runs SET reconciliation_deadline=now()-interval '1 second' WHERE id=$1",UUID(r['run_id']));s=await t.wait(r);assert s['status']=='partial' and s['requires_manual_reconciliation'];assert (await t.counts(a['action_id']))['attempts']==1
            return dict(run=r,manual_reconciliation=True,budget_expiry_injected=True,zero_resend=True)
        await t.record('Unknown with no receipt stays uncertain and reaches partial/manual',unresolved)
        async def cancellation():
            r,a=await t.pending('重启 s07-demo');before=await t.health();response=await client.post(BASE+'/api/v1/agent/runs/'+r['run_id']+'/cancel',headers=t.h());assert response.status_code==200
            s=await t.wait(r);assert s['status']=='cancelled';assert (await t.health())['generation']==before['generation'];return dict(run=r,counts=await t.counts(a['action_id']))
        await t.record('Cancel before consumption invalidates same authority approval',cancellation)
        async def bypass():
            from ops_agent.service_contract import sign_grant
            g=dict(aud='ma-ops-execute/v1',iat=int(time.time()),exp=int(time.time())+60,owner_id=70001,run_id=str(uuid4()),action_id=str(uuid4()),execution_ref=str(uuid4()),capability='service.restart',typed_args={'asset_id':'s07-demo'},target_version='a'*64,plan_hash='b'*64,policy_version='v4-s07-local/v1')
            snap=await client.post('http://127.0.0.1:28975/internal/ma-ops-exec/v1/snapshot',json={'asset_id':'s07-demo'},headers={'X-PowerSystem-Token':'s07-local-adapter-token-000000000000000'});g['target_version']=snap.json()['target_version']
            response=await client.post('http://127.0.0.1:28975/internal/ma-ops-exec/v1/execute',json={'grant':sign_grant(g,'s07-local-adapter-grant-000000000000000')},headers={**t.h(),'X-PowerSystem-Token':'s07-local-adapter-token-000000000000000'})
            assert response.status_code in (403,409),response.text
            response2=await client.post('http://127.0.0.1:28973/internal/ma/v1/assets/import',json={},headers={'X-PowerSystem-Token':'s07-local-adapter-token-000000000000000'})
            assert response2.status_code==401
            return dict(unbacked_signed_grant_denied=True,adapter_cannot_use_authority_service_identity=True)
        await t.record('Signed but unbacked grant and adapter identity escalation rejected',bypass)
        t.save();print(json.dumps(dict(result=str(t.path),checks=len(t.checks),passed=all(v['passed'] for v in t.checks))),flush=True)
    await db.close()

if __name__=='__main__':asyncio.run(main())

