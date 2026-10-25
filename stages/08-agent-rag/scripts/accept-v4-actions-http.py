"""Necessary S06 HTTP/DB acceptance only. Never runs without --run."""
import asyncio,json,sys,time,hashlib
from pathlib import Path
from uuid import uuid4,UUID
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'artifacts/v4-assistant/s06-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
import httpx,asyncpg
from ops_agent.security import claims,sign
from rag_agent.ops import envelope_signature
from ops_agent.business_contract import canonical,digest,sign_grant

BASE='http://127.0.0.1:28962';DSN='postgresql://s06_test@127.0.0.1:25436/v4_s06_20261024_r1_test'
ACTOR='s06-local-synthetic-actor-key-000000000';TOKEN='s06-local-synthetic-ops-token-000000000';ENV='s06-local-synthetic-envelope-key-000000'
TERMINAL=('succeeded','failed','partial','cancelled')

class Check:
    def __init__(self,c,db):self.c,self.db,self.tokens,self.runs,self.checks=c,db,{},[],[]
    async def login(self,owner=60001):
        r=await self.c.post(BASE+'/api/v1/auth/login',json=dict(username='s06-user-'+str(owner),password='S06-local-only-2026!'));r.raise_for_status();self.tokens[owner]=r.json()['data']['token']
    def h(self,owner=60001):return {'Authorization':'Bearer '+self.tokens[owner]}
    async def mode(self,mode):r=await self.c.post('http://127.0.0.1:28964/drill/mode',json={'mode':mode});r.raise_for_status()
    async def fault(self,mode):r=await self.c.post(BASE+'/drill/fault',json={'mode':mode});r.raise_for_status()
    async def create(self,message,owner=60001,session=None,key=None):
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
    async def pending(self,message,owner=60001,session=None):
        run=await self.create(message,owner,session);value=await self.wait(run,('awaiting_approval',*TERMINAL));assert value['status']=='awaiting_approval',value;return run,value['actions'][0]
    async def decide(self,run,a,decision='approve',owner=None,**changes):
        return await self.c.post(BASE+'/api/v1/agent/runs/'+run['run_id']+'/actions/'+a['action_id']+'/decision',headers=self.h(owner or run['owner']),json=dict(approval_id=a['approval_id'],plan_hash=a['plan_hash'],decision=decision,**changes))
    async def counts(self,aid):
        return dict(await self.db.fetchrow('''SELECT (SELECT count(*) FROM ma_approvals WHERE task_id=$1) approvals,(SELECT count(*) FROM ma_execution_attempts WHERE task_id=$1) attempts,(SELECT count(*) FROM business_action_receipts WHERE action_id=$1) receipts''',UUID(aid)))
    async def record(self,name,operation):
        before=time.monotonic()
        try:
            result=await operation();self.checks.append(dict(case=name,passed=True,duration_ms=int((time.monotonic()-before)*1000),evidence=result));print('PASS',name,flush=True)
        except Exception as e:self.checks.append(dict(case=name,passed=False,error_type=type(e).__name__,error=str(e)[:3500]));print('FAIL',name,str(e)[:300],flush=True)
        finally:
            await self.fault('none');await self.mode('allow')
            await self.db.execute("UPDATE users SET role='operator' WHERE id=60004")
            self.save()
    def save(self):
        payload=dict(phase='V4-S06',environment='dedicated PG25436; actual JWT/HTTP/runs/ma authority/business transactions',candidate_mode='synthetic decision fixture except separately labelled official batches',checks=self.checks,runs=self.runs,passed=all(c['passed'] for c in self.checks),at=datetime.now(timezone.utc).isoformat())
        self.path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str),encoding='utf8')

async def main():
    if '--run' not in sys.argv:raise SystemExit('--run required')
    db=await asyncpg.connect(DSN)
    async with httpx.AsyncClient(timeout=45,trust_env=False) as client:
        t=Check(client,db);t.path=OUT/('technical-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
        for user in (60001,60002,60003,60004):await t.login(user)
        async def r0():
            await t.mode('unavailable');before=await db.fetchval('SELECT count(*) FROM ma_security_reviews')
            r=await t.create('普通读取');s=await t.wait(r);assert s['status']=='succeeded',s;assert '12.5' in s['final_answer'];assert before==await db.fetchval('SELECT count(*) FROM ma_security_reviews')
            steps=await db.fetchval("SELECT count(*) FROM assistant_run_steps WHERE run_id=$1 AND kind='read_tool' AND state='completed'",UUID(r['run_id']));assert steps==1
            return dict(run=r,fast_read_audited=True,security_calls=0)
        await t.record('R0 actual ordinary read survives security outage and is audited',r0)
        async def unavailable():
            await t.mode('unavailable');results=[]
            for text in ('备注 602 0 unavailable','读取备注 601'):
                r=await t.create(text);s=await t.wait(r);assert s['status']=='failed';a=s['actions'][0];assert a['status']=='blocked' and a['error_code']=='security_unavailable';assert (await t.counts(a['action_id']))==dict(approvals=0,attempts=0,receipts=0);results.append(dict(run=r,action=a['action_id']))
            return results
        await t.record('Required independent review unavailable blocks sensitive read and R1',unavailable)
        async def deny():
            await t.mode('deny');r=await t.create('备注 602 0 deny');s=await t.wait(r);assert s['status']=='failed' and s['actions'][0]['error_code']=='security_denied';assert (await t.counts(s['actions'][0]['action_id']))['receipts']==0;return dict(run=r,dangerous_effects=0)
        await t.record('Independent model deny blocks operation',deny)
        async def normal():
            run,a=await t.pending('备注 602 0 approved-once');counts=await t.counts(a['action_id']);assert counts==dict(approvals=1,attempts=0,receipts=0)
            results=await asyncio.gather(t.decide(run,a),t.decide(run,a));assert all(r.status_code==200 for r in results),[r.text for r in results]
            final=await t.wait(run);assert final['status']=='succeeded',final;counts=await t.counts(a['action_id']);assert counts==dict(approvals=1,attempts=1,receipts=1)
            repeat=await t.decide(run,a);assert repeat.status_code==200 and repeat.json()['actions'][0]['execution_ref']==final['actions'][0]['execution_ref'];conflict=await t.decide(run,a,'reject');assert conflict.status_code==409
            device=await db.fetchrow('SELECT remark,remark_version,remark_updated_by FROM devices WHERE id=602');assert dict(device)==dict(remark='approved-once',remark_version=1,remark_updated_by=60001)
            receipt=await db.fetchrow('SELECT before_hash,after_hash FROM business_action_receipts WHERE action_id=$1',UUID(a['action_id']));assert receipt['before_hash']!=receipt['after_hash']
            return dict(run=run,execution_ref=final['actions'][0]['execution_ref'],counts=counts,concurrent_replay=True,conflict_http=409,device=dict(device))
        await t.record('R1 explicit approve, concurrent duplicate/replay exactly once and transaction audit',normal)
        async def reject():
            r,a=await t.pending('备注 603 0 rejected');response=await t.decide(r,a,'reject');assert response.status_code==200
            s=await t.wait(r);assert s['status']=='cancelled';again=await t.decide(r,a,'reject');assert again.status_code==200;assert (await t.decide(r,a)).status_code==409;assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,no_effect=True)
        await t.record('Reject terminal and idempotent, no side effect',reject)
        async def binding_paths():
            r,a=await t.pending('备注 603 0 binding')
            for user in (60002,60003):
                assert (await client.get(BASE+'/api/v1/agent/runs/'+r['run_id'],headers=t.h(user))).status_code==404
                assert (await t.decide(r,a,owner=user)).status_code==404
            wrong=dict(a,plan_hash='0'*64);assert (await t.decide(r,wrong)).status_code==409
            wrong=dict(a,approval_id=str(uuid4()));assert (await t.decide(r,wrong)).status_code==409
            assert (await t.decide(r,a,typed_args={'device_id':604})).status_code==400
            other=await t.create('自然解释');await t.wait(other)
            assert (await client.post(BASE+'/api/v1/agent/runs/'+other['run_id']+'/actions/'+a['action_id']+'/decision',headers=t.h(),json=dict(decision='approve',approval_id=a['approval_id'],plan_hash=a['plan_hash']))).status_code==404
            assert (await t.counts(a['action_id']))['attempts']==0;await t.decide(r,a,'reject');return dict(run=r,cross_user_and_admin_denied=True,binding_variations_denied=True)
        await t.record('Owner/run/action/approval/hash binding, unknown parameters and cross-user/admin denied',binding_paths)
        async def expiry():
            r,a=await t.pending('备注 603 0 expired');await db.execute("UPDATE ma_approvals SET expires_at=now()-interval '1 second' WHERE task_id=$1",UUID(a['action_id']))
            response=await t.decide(r,a);assert response.status_code==409;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,http=409)
        await t.record('Expired approval cannot execute and authority projection converges',expiry)
        async def version():
            r,a=await t.pending('备注 604 0 stale-target');await db.execute('UPDATE devices SET remark_version=remark_version+1 WHERE id=604')
            response=await t.decide(r,a);assert response.status_code==409 and response.json()['error']['code']=='target_version_changed',response.text;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,synthetic_version_change=True,http=409)
        await t.record('Target version change invalidates approval',version)
        async def args_change():
            r,a=await t.pending('备注 605 0 tampered-plan')
            await db.execute("UPDATE ma_approvals SET plan=jsonb_set(plan,'{binding,typed_args,remark}','\"changed-by-test\"') WHERE task_id=$1",UUID(a['action_id']))
            response=await t.decide(r,a);assert response.status_code==409;await t.wait(r);assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,immutable_material_tampering_detected=True)
        await t.record('Changed parameter material cannot reuse prior hash/approval',args_change)
        async def revoke():
            r,a=await t.pending('备注 606 0 revoked',60004);await db.execute("UPDATE users SET role='viewer' WHERE id=60004")
            response=await t.decide(r,a);assert response.status_code in (401,403)
            await db.execute("UPDATE users SET role='operator' WHERE id=60004");await t.decide(r,a,'reject');assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,rechecked_current_db_role=True)
        await t.record('Role revocation before decision denies stale JWT',revoke)
        async def revoke_dispatch():
            await t.fault('pause');r,a=await t.pending('备注 607 0 revoked-at-dispatch',60004);assert (await t.decide(r,a)).status_code==200
            for _ in range(60):
                if (await client.get(BASE+'/drill/fault')).json()['arrived']:break
                await asyncio.sleep(.05)
            else:raise AssertionError('no dispatch')
            await db.execute("UPDATE users SET role='viewer' WHERE id=60004");await t.fault('none');await asyncio.sleep(.3);await db.execute("UPDATE users SET role='operator' WHERE id=60004")
            s=await t.wait(r);assert s['status']=='failed',s;assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=1,receipts=0);assert await db.fetchval('SELECT remark_version FROM devices WHERE id=607')==0;return dict(run=r,execution_preflight_rechecked=True)
        await t.record('Role revocation after approval before business handler prevents side effect',revoke_dispatch)
        async def cancel_pending():
            r,a=await t.pending('备注 608 0 cancel-pending');response=await client.post(BASE+'/api/v1/agent/runs/'+r['run_id']+'/cancel',headers=t.h());assert response.status_code==200
            s=await t.wait(r);assert s['status']=='cancelled';assert (await t.counts(a['action_id']))['attempts']==0;return dict(run=r,authority_status=s['actions'][0]['status'])
        await t.record('Cancel awaiting approval competes with consumption in same authority ledger',cancel_pending)
        async def alarm():
            r,a=await t.pending('确认告警 60609');assert (await t.decide(r,a)).status_code==200;s=await t.wait(r);assert s['status']=='succeeded' and '不代表' in s['final_answer'];row=await db.fetchrow('SELECT status,acked_by,recovered_at FROM alarm_records WHERE id=60609');assert row['status']=='acked' and row['acked_by']==60001 and row['recovered_at'] is None
            notifications=await db.fetchval("SELECT count(*) FROM business_notification_events WHERE alarm_id=60609") if await db.fetchval("SELECT to_regclass('business_notification_events') IS NOT NULL") else None
            return dict(run=r,alarm=dict(row),notification_count=notifications,counts=await t.counts(a['action_id']))
        await t.record('Alarm acknowledgement preserves existing semantics, never means recovery',alarm)
        async def sensitive():
            r=await t.create('读取备注 602');s=await t.wait(r);assert s['status']=='succeeded' and s['actions'][0]['display_result']=='approved-once';a=s['actions'][0]
            assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=1,receipts=1)
            steps=await db.fetch('SELECT output,result_state FROM assistant_run_steps WHERE run_id=$1 AND kind=\'model\'',UUID(r['run_id']));assert all('approved-once' not in str(x) for x in steps)
            public=await client.get(BASE+'/api/v1/devices/602',headers=t.h());assert 'approved-once' not in public.text and 'remark_version' in public.text
            return dict(run=r,reviewed_sensitive_read=True,model_received_content=False,ordinary_read_hides_remark=True)
        await t.record('Sensitive read independently reviewed, text stays platform-only',sensitive)
        async def execution_fail():
            await t.fault('fail');r,a=await t.pending('备注 610 0 synthetic-execution-failure');assert (await t.decide(r,a)).status_code==200;s=await t.wait(r);assert s['status']=='failed' and s['actions'][0]['no_effect'];assert (await t.counts(a['action_id']))['receipts']==0;return dict(run=r,known_no_effect=True)
        await t.record('Explicit execution failure retains consumed approval and no-effect audit',execution_fail)
        async def unknown_reconcile():
            await t.fault('drop-result');r,a=await t.pending('备注 611 0 committed-response-lost');assert (await t.decide(r,a)).status_code==200
            s=await t.wait(r,('reconciling',*TERMINAL));assert s['status']=='reconciling',s
            for _ in range(60):
                if (await t.counts(a['action_id']))['receipts']==1:break
                await asyncio.sleep(.1)
            response=await client.post(BASE+'/api/v1/agent/runs/'+r['run_id']+'/cancel',headers=t.h());assert response.status_code in (200,202);assert response.json()['status'] not in ('cancelled','succeeded')
            await t.fault('none');s=await t.wait(r);assert s['status']=='partial' and s['cancel_requested'] and not s['requires_manual_reconciliation'];assert s['actions'][0]['status']=='succeeded';assert (await t.counts(a['action_id']))==dict(approvals=1,attempts=1,receipts=1)
            return dict(run=r,final=s['status'],actual_side_effect=True,reconciled_from_receipt=True,no_resend=True)
        await t.record('Committed write with lost result reconciles by receipt; cancel never claims rollback',unknown_reconcile)
        async def unresolved():
            await t.fault('unresolved');r,a=await t.pending('备注 612 0 unknown-no-receipt');assert (await t.decide(r,a)).status_code==200;await t.wait(r,('reconciling',*TERMINAL))
            await db.execute("UPDATE assistant_runs SET reconciliation_deadline=now()-interval '1 second' WHERE id=$1",UUID(r['run_id']));s=await t.wait(r);assert s['status']=='partial' and s['requires_manual_reconciliation'];assert (await t.counts(a['action_id']))['attempts']==1
            return dict(run=r,budget_expiry_injected=True,manual_reconciliation=True,no_resend=True)
        await t.record('Unknown result without receipt reaches partial/manual after reconciliation budget',unresolved)
        async def target_concurrent():
            r1,a1=await t.pending('备注 613 0 concurrent-one');r2,a2=await t.pending('备注 613 0 concurrent-two',60002)
            await asyncio.gather(t.decide(r1,a1),t.decide(r2,a2));s1,s2=await asyncio.gather(t.wait(r1),t.wait(r2));assert sorted([s1['status'],s2['status']])==['failed','succeeded'],[s1,s2]
            assert await db.fetchval('SELECT remark_version FROM devices WHERE id=613')==1
            counts=[await t.counts(a1['action_id']),await t.counts(a2['action_id'])];assert sum(c['receipts'] for c in counts)==1;return dict(runs=[r1,r2],counts=counts,single_version_increment=True)
        await t.record('Concurrent actions on one version serialize to one business update',target_concurrent)
        async def hard_deny():
            from ops_agent.runtime_reviews import contract
            before=await db.fetchval('SELECT count(*) FROM business_action_receipts');outcomes=[]
            for capability,data,destination,risk in [('device.control','internal','platform_client','R2'),('device.remark.read','secret','platform_client','R0'),('device.remark','internal','external','R1')]:
                rid=uuid4();raw=dict(protocol='ma-action-review/v1',review_id=str(rid),binding_hash='a'*64,capability=capability,data_class=data,destination=destination,risk_class=risk,owner_id=60001,policy_version='v4-s06-local/v1',target_version='0',argument_hash='b'*64,remark_length=0,expires_at=int(time.time())+120)
                response=await client.post('http://127.0.0.1:28964/internal/ma-action-review/v1/reviews',json=raw,headers={'X-SEC-Service-Token':'s06-local-synthetic-sec-token-000000000','X-SEC-Request':contract.sign_message('s06-local-synthetic-sec-request-0000000','ma-action-review-request/v1',rid,contract.digest(raw),raw['expires_at'])})
                assert response.status_code==200 and response.json()['decision']['decision']=='deny';outcomes.append(dict(capability=capability,data=data,destination=destination,decision='deny'))
            assert before==await db.fetchval('SELECT count(*) FROM business_action_receipts')
            return dict(outcomes=outcomes,dangerous_effects=0,model_allow_cannot_override=True)
        await t.record('Hard forbidden capabilities, secrets and external destinations deterministically deny',hard_deny)
        async def raw_bypass():
            rid,aid,eid=uuid4(),uuid4(),uuid4();g=dict(aud='ma-business-execute/v1',iat=int(time.time()),exp=int(time.time())+60,owner_id=60001,run_id=str(rid),action_id=str(aid),execution_ref=str(eid),capability='device.remark',typed_args=dict(device_id=614,remark='should-never-write',expected_version=0),target_version='0',plan_hash='0'*64,policy_version='v4-s06-local/v1')
            # Even an internally signed grant without an existing consumed ledger fails.
            response=await client.post(BASE+'/internal/ma-action/v1/execute',headers={**t.h(),'X-PowerSystem-Token':TOKEN},json={'grant':sign_grant(g,ACTOR)});assert response.status_code==403
            response=await client.patch(BASE+'/api/v1/devices/614/remark',headers=t.h(),json=dict(remark='bypass',expected_version=0));assert response.status_code==403
            assert await db.fetchval('SELECT remark_version FROM devices WHERE id=614')==0
            return dict(unbacked_signed_grant_denied=True,naked_patch_denied=True,dangerous_effects=0)
        await t.record('Direct write and signed-grant bypass need consumed authority claim',raw_bypass)
        t.save();print(json.dumps(dict(result=str(t.path),passed=all(c['passed'] for c in t.checks),checks=len(t.checks))))
    await db.close()

if __name__=='__main__':asyncio.run(main())
