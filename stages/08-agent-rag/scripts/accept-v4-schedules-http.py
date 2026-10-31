"""Necessary S08 HTTP/ledger checks; --run explicitly opts into the dedicated DB."""
import asyncio,json,sys,time,hashlib,re
from pathlib import Path
from uuid import UUID,uuid4
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[3];OUT=ROOT/'artifacts/v4-智能助手/S08-定期任务-20261024-r1'
for p in ('08-agent-rag','09-ops-agent','10-security-agent'):sys.path.insert(0,str(ROOT/'stages'/p))
sys.path.insert(0,str(OUT/'runtime-deps'))
import asyncpg,httpx
from rag_agent.schedules import ScheduleConfig
BASE='http://127.0.0.1:28982';DSN='postgresql://s08_test@127.0.0.1:25438/v4_s08_20261024_r1_test';TERMINAL=('succeeded','partial','failed','cancelled')
class Check:
    def __init__(self,c,db):self.c,self.db=c,db;self.tokens={};self.checks=[];self.runs=[];self.path=OUT/('technical-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json')
    async def login(self,owner=80001):
        r=await self.c.post(BASE+'/api/v1/auth/login',json=dict(username='s08-user-'+str(owner),password='S08-local-only-2026!'));r.raise_for_status();self.tokens[owner]=r.json()['data']['token']
    def h(self,owner=80001):return dict(Authorization='Bearer '+self.tokens[owner])
    async def mode(self,mode):r=await self.c.post('http://127.0.0.1:28984/drill/mode',json=dict(mode=mode));r.raise_for_status()
    async def get(self,run):
        r=await self.c.get(BASE+'/api/v1/agent/runs/'+run['run_id'],headers=self.h(run.get('owner',80001)));r.raise_for_status();return r.json()
    async def wait(self,run,states=TERMINAL,seconds=40):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            v=await self.get(run)
            if v['status'] in states:return v
            await asyncio.sleep(.15)
        raise AssertionError('run timeout '+json.dumps(v,ensure_ascii=False))
    async def create(self,config=None,key=None,owner=80001):
        config=config or ScheduleConfig(task_type='daily_report',station_code='PV-01').model_dump()
        r=await self.c.post(BASE+'/api/v1/agent/schedules',headers={**self.h(owner),'Idempotency-Key':str(key or uuid4())},json=dict(config=config));r.raise_for_status();value={**r.json(),'owner':owner};self.runs.append(value);return value
    async def pending(self,config=None):
        r=await self.create(config);v=await self.wait(r,('awaiting_approval',*TERMINAL));assert v['status']=='awaiting_approval',v;return r,v['actions'][0]
    async def decide(self,run,a,decision='approve',owner=None,**extra):
        return await self.c.post(BASE+'/api/v1/agent/runs/'+run['run_id']+'/actions/'+a['action_id']+'/decision',headers=self.h(owner or run.get('owner',80001)),json=dict(decision=decision,approval_id=a['approval_id'],plan_hash=a['plan_hash'],**extra))
    async def schedule(self,sid,owner=80001):
        r=await self.c.get(BASE+'/api/v1/agent/schedules/'+sid,headers=self.h(owner));r.raise_for_status();return r.json()
    async def active(self,config=None):
        r,a=await self.pending(config);v=await self.decide(r,a);assert v.status_code==200,v.text
        result=await self.wait(r);assert result['status']=='succeeded',result
        return r,await self.schedule(r['schedule_id'])
    async def control(self,sid,op):
        r=await self.c.post(BASE+'/api/v1/agent/schedules/'+sid+'/'+op,headers={**self.h(),'Idempotency-Key':str(uuid4())},json={});r.raise_for_status();return r.json()
    async def due(self,sid,when=None,concurrency=8):
        now=when or datetime.now(timezone.utc)
        await self.db.execute('UPDATE assistant_schedules SET next_run_at=$2 WHERE id=$1',UUID(sid),now)
        r=await self.c.post('http://127.0.0.1:28981/drill/tick',json=dict(now=now.isoformat(),concurrency=concurrency));r.raise_for_status()
        row=await self.db.fetchrow('SELECT * FROM assistant_schedule_slots WHERE schedule_id=$1 AND scheduled_for=$2',UUID(sid),now)
        assert row,row
        return dict(row)
    async def counts(self,aid):return dict(await self.db.fetchrow('SELECT (SELECT count(*) FROM ma_approvals WHERE task_id=$1) approvals,(SELECT count(*) FROM ma_execution_attempts WHERE task_id=$1) attempts,(SELECT count(*) FROM assistant_schedule_receipts WHERE action_id=$1) receipts',UUID(aid)))
    async def case(self,name,operation):
        selected=next((sys.argv[i+1] for i,x in enumerate(sys.argv[:-1]) if x=='--cases'),None)
        if selected and not re.search(selected,name):return
        try:value=await operation();self.checks.append(dict(case=name,passed=True,evidence=value));print('PASS',name,flush=True)
        except Exception as e:self.checks.append(dict(case=name,passed=False,error_type=type(e).__name__,error=str(e)[:4500]));print('FAIL',name,str(e)[:450],flush=True)
        finally:
            await self.mode('allow')
            await self.db.execute("UPDATE users SET role=CASE WHEN id=80003 THEN 'admin' ELSE 'operator' END WHERE id IN (80001,80002,80003)")
            self.save()
    def save(self):self.path.write_text(json.dumps(dict(phase='V4-S08',environment='dedicated PG25438, actual JWT/HTTP/runs/original stage9 authority, explicit synthetic historical TD transport',candidate_mode='synthetic reviewer except separately labelled official acceptance',checks=self.checks,runs=self.runs,passed=all(x['passed'] for x in self.checks),at=datetime.now(timezone.utc).isoformat()),ensure_ascii=False,indent=2,default=str),encoding='utf8')
async def main():
    if '--run' not in sys.argv:raise SystemExit('--run required')
    db=await asyncpg.connect(DSN)
    async with httpx.AsyncClient(timeout=45,trust_env=False) as client:
        t=Check(client,db)
        for owner in (80001,80002,80003):await t.login(owner)
        await t.mode('allow')
        async def once():
            key=uuid4();a,b=await asyncio.gather(t.create(key=key),t.create(key=key));assert a['run_id']==b['run_id']
            v=await t.wait(a,('awaiting_approval',*TERMINAL));assert v['status']=='awaiting_approval',v
            action=v['actions'][0];assert await t.counts(action['action_id'])==dict(approvals=1,attempts=0,receipts=0)
            assert not await db.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_schedules WHERE id=$1)',UUID(a['schedule_id']))
            response=await asyncio.gather(t.decide(a,action),t.decide(a,action));assert all(r.status_code==200 for r in response),[r.text for r in response]
            s=await t.wait(a);assert s['status']=='succeeded',s
            assert await t.counts(action['action_id'])==dict(approvals=1,attempts=1,receipts=1)
            config=await t.schedule(a['schedule_id']);assert config['config_version']==1 and config['grant']['revoked_at'] is None
            assert (await t.decide(a,action)).status_code==200
            assert (await t.decide(a,action,'reject')).status_code==409
            await t.control(a['schedule_id'],'cancel')
            return dict(run=a,counts=await t.counts(action['action_id']),grant=config['grant'])
        await t.case('Concurrent configuration idempotency and one-time approval produce one finite grant',once)
        async def rejects():
            output=[]
            for mode in ('deny','unavailable','allow'):
                await t.mode(mode);r=await t.create();s=await t.wait(r,('awaiting_approval',*TERMINAL))
                if mode=='allow':assert (await t.decide(r,s['actions'][0],'reject')).status_code==200;await t.wait(r)
                else:assert s['status']=='failed',s
                assert not await db.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_schedules WHERE id=$1)',UUID(r['schedule_id']));output.append(r)
            return output
        await t.case('Independent deny unavailable and owner reject create no schedule or grant',rejects)
        async def bounds():
            r,a=await t.pending()
            for owner in (80002,80003):
                assert (await t.decide(r,a,owner=owner)).status_code==404
                assert (await client.get(BASE+'/api/v1/agent/runs/'+r['run_id'],headers=t.h(owner))).status_code==404
            assert (await t.decide(r,{**a,'plan_hash':'0'*64})).status_code==409
            assert (await t.decide(r,a,typed_args={'x':1})).status_code==400
            await db.execute("UPDATE ma_approvals SET expires_at=now()-interval '1 second' WHERE task_id=$1",UUID(a['action_id']))
            assert (await t.decide(r,a)).status_code==409
            await t.wait(r)
            assert (await t.counts(a['action_id']))['attempts']==0
            return dict(run=r,owner_and_admin_isolation=True,expiry_and_arguments_checked=True)
        await t.case('Owner isolation forged approval unknown parameters and expired approval denied',bounds)
        async def read_delivery():
            values=[]
            for task in ('daily_report','inspection'):
                r,s=await t.active(ScheduleConfig(task_type=task,station_code='PV-01').model_dump())
                await t.mode('unavailable')
                slot=await t.due(s['id']);assert slot['run_id']
                receipt=dict(run_id=str(slot['run_id']),owner=80001)
                done=await t.wait(receipt);assert done['status']=='succeeded',done
                assert not done['actions'] and len(done['artifacts'])==2
                for file in done['artifacts']:
                    download=await client.get(BASE+'/api/v1/agent/artifacts/'+file['artifact_id']+'/download',headers=t.h());assert download.status_code==200
                    assert hashlib.sha256(download.content).hexdigest()==file['sha256']
                    assert (await client.get(BASE+'/api/v1/agent/artifacts/'+file['artifact_id']+'/download',headers=t.h(80003))).status_code==404
                count=await db.fetchval('SELECT count(*) FROM assistant_schedule_slots WHERE schedule_id=$1 AND scheduled_for=$2',UUID(s['id']),slot['scheduled_for']);assert count==1
                assert not await db.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_actions WHERE run_id=$1)',slot['run_id'])
                steps=[dict(x) for x in await db.fetch('SELECT kind,attempts FROM assistant_run_steps WHERE run_id=$1',slot['run_id'])]
                assert all(x['kind']=='read_tool' for x in steps),steps
                await t.control(s['id'],'cancel');await t.mode('allow');values.append(dict(config_run=r,slot=slot,run=done,steps=steps))
            return values
        await t.case('Concurrent slot uniqueness read-only daily and inspection actual files and owner download',read_delivery)
        async def pause():
            r,s=await t.active();await t.mode('unavailable')
            first=await t.control(s['id'],'pause');second=await t.control(s['id'],'pause');assert first['status']==second['status']=='paused'
            assert first['grant']['revoked_at']
            count=await db.fetchval('SELECT count(*) FROM assistant_schedule_slots WHERE schedule_id=$1',UUID(s['id']))
            await client.post('http://127.0.0.1:28981/drill/tick',json={})
            assert await db.fetchval('SELECT count(*) FROM assistant_schedule_slots WHERE schedule_id=$1',UUID(s['id']))==count
            response=await client.post(BASE+'/api/v1/agent/schedules/'+s['id']+'/resume',headers={**t.h(),'Idempotency-Key':str(uuid4())},json=dict(expected_version=s['config_version']));response.raise_for_status()
            failed=await t.wait(response.json());assert failed['status']=='failed'
            assert (await t.schedule(s['id']))['status']=='paused'
            final=await t.control(s['id'],'cancel');assert final['status']=='cancelled'
            return dict(run=r,paused=first,cancelled=final,no_review_for_reduction=True)
        await t.case('Pause cancel idempotent without reviewer and resume requires new valid review',pause)
        async def drift():
            values=[]
            for mode in ('revoked','expired','version','scope','role'):
                r,s=await t.active();sid=UUID(s['id'])
                if mode=='revoked':await db.execute('UPDATE assistant_schedule_grants SET revoked_at=now() WHERE schedule_id=$1',sid)
                if mode=='expired':await db.execute("UPDATE assistant_schedule_grants SET expires_at=now()-interval '1 second',approved_at=now()-interval '2 seconds' WHERE schedule_id=$1",sid)
                if mode=='version':await db.execute('UPDATE assistant_schedules SET config_version=config_version+1 WHERE id=$1',sid)
                if mode=='scope':await db.execute("UPDATE assistant_schedules SET config=jsonb_set(config,'{scope}','\"all\"') WHERE id=$1",sid)
                if mode=='role':await db.execute("UPDATE users SET role='viewer' WHERE id=80001")
                response=await client.post('http://127.0.0.1:28981/drill/tick',json={});assert response.status_code==200,response.text
                saved=await db.fetchrow('SELECT status,reason FROM assistant_schedules WHERE id=$1',sid);assert saved['status']=='paused',saved
                assert not await db.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_schedule_slots WHERE schedule_id=$1)',sid)
                await db.execute("UPDATE users SET role='operator' WHERE id=80001")
                values.append(dict(mode=mode,schedule=s['id'],state=dict(saved)))
            return values
        await t.case('Grant revocation expiry configuration version scope drift and current role pause before execution',drift)
        async def missed():
            r,s=await t.active(ScheduleConfig(task_type='inspection',mode='interval',daily_time=None,interval_minutes=15).model_dump())
            at=datetime.now(timezone.utc);due=at-timedelta(hours=2)
            await db.execute('UPDATE assistant_schedules SET next_run_at=$2 WHERE id=$1',UUID(s['id']),due)
            response=await client.post('http://127.0.0.1:28981/drill/tick',json=dict(now=at.isoformat(),concurrency=10));response.raise_for_status()
            row=await db.fetchrow('SELECT * FROM assistant_schedule_slots WHERE schedule_id=$1',UUID(s['id']));assert row['status']=='skipped' and row['run_id'] is None and row['skipped_count']>=8
            await t.control(s['id'],'cancel');return dict(row)
        await t.case('Missed slots recorded skipped without catchup or burst',missed)
        async def preview():
            before=await db.fetchval("SELECT md5(coalesce(jsonb_agg(to_jsonb(r) ORDER BY id)::text,'')) FROM alarm_rules r")
            raw=dict(device_id=801,metric='temperature',start='2026-10-23T00:00:00+08:00',end='2026-10-23T00:00:30+08:00',operator='gt',threshold=70,baseline_threshold=75,expected_interval_seconds=5)
            response=await client.post(BASE+'/api/v1/agent/rule-preview',headers={**t.h(),'Idempotency-Key':str(uuid4())},json=raw);response.raise_for_status();r=response.json()
            done=await t.wait(r);assert done['status']=='succeeded',done
            file=done['artifacts'][0];download=await client.get(BASE+'/api/v1/agent/artifacts/'+file['artifact_id']+'/download',headers=t.h());assert download.status_code==200
            value=download.json();assert value['coverage']['valid_samples']==5 and value['coverage']['missing_samples']==1
            assert value['metrics']['baseline_hits']==2 and value['metrics']['candidate_hits']==4
            after=await db.fetchval("SELECT md5(coalesce(jsonb_agg(to_jsonb(r) ORDER BY id)::text,'')) FROM alarm_rules r");assert before==after
            assert not await db.fetchval('SELECT EXISTS(SELECT 1 FROM ma_tasks WHERE run_id=$1)',UUID(r['run_id']))
            return dict(run=r,preview=value,rule_table_unchanged=True)
        await t.case('Historical snapshot replay actual HTTP file coverage missing and zero rule mutation',preview)
        async def invalid():
            checks=[]
            for config in (dict(task_type='service.restart'),dict(task_type='daily_report',destination_id='external'),dict(task_type='daily_report',grant_days=91),dict(task_type='inspection',mode='interval',interval_minutes=14,daily_time=None)):
                r=await client.post(BASE+'/api/v1/agent/schedules',headers={**t.h(),'Idempotency-Key':str(uuid4())},json=dict(config=config));assert r.status_code in (400,422);checks.append(r.status_code)
            r=await client.post(BASE+'/api/v1/agent/schedules',headers={**t.h(),'Idempotency-Key':str(uuid4())},json=dict(config=ScheduleConfig(task_type='inspection').model_dump(),user_id=80003));assert r.status_code==400
            raw=await client.get(BASE+'/internal/agent/schedule-read/'+str(uuid4()),params=dict(grant_id=str(uuid4()),kind='energy'),headers=t.h());assert raw.status_code==403
            return dict(invalid_configs=checks,forged_owner=400,no_service_identity=403)
        await t.case('R1 R2 external arbitrary configuration owner injection and naked bridge bypass denied',invalid)
        t.save();print(str(t.path));await t.mode('official')
    await db.close()
if __name__=='__main__':asyncio.run(main())
