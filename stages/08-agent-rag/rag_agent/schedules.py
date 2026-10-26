"""Finite owner-only read schedules. Action approvals stay exclusively in stage9."""
import asyncio,hashlib,json
from datetime import datetime,timezone,timedelta
from urllib.parse import urlsplit
from uuid import UUID,uuid4,uuid5,NAMESPACE_URL
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field,model_validator
from .ma_security import digest,verify
from .run_store import encode,unpack
from .run_worker import AuthVault,run_profile
from .schedule_time import next_slot,validate_clock,missed_count
from .artifacts import ArtifactService,ReportArgs,metadata

POLICY='v4-s08-local/v1'
class Strict(BaseModel):model_config=ConfigDict(extra='forbid')
class ScheduleConfig(Strict):
    task_type:Literal['daily_report','inspection']
    scope:Literal['all','business','baseline']='business'
    station_code:str|None=Field(default=None,pattern='^[A-Za-z0-9_-]{1,64}$')
    device_id:int|None=Field(default=None,gt=0,le=9007199254740991,strict=True)
    timezone:str=Field(default='Asia/Shanghai',min_length=1,max_length=64)
    mode:Literal['daily','interval']='daily'
    daily_time:str|None='09:00'
    interval_minutes:int|None=Field(default=None,ge=15,le=10080,strict=True)
    grant_days:int=Field(default=30,ge=1,le=90,strict=True)
    destination_id:Literal['platform_client']='platform_client'
    destination_version:Literal['1']='1'
    @model_validator(mode='after')
    def clock(self):validate_clock(self.model_dump());return self
class Create(Strict):config:ScheduleConfig
class Update(Create):expected_version:int=Field(gt=0,le=9007199254740991,strict=True)
class Resume(Strict):expected_version:int=Field(gt=0,le=9007199254740991,strict=True)
class Empty(Strict):pass
class Preview(Strict):
    device_id:int=Field(gt=0,le=9007199254740991,strict=True)
    metric:Literal['voltage','current','temperature','power']
    start:str;end:str
    operator:Literal['gt','gte','lt','lte']
    threshold:int=Field(ge=-1000000,le=1000000,strict=True)
    baseline_threshold:int=Field(ge=-1000000,le=1000000,strict=True)
    expected_interval_seconds:int=Field(default=5,ge=1,le=86400,strict=True)
    @model_validator(mode='after')
    def window(self):
        a,b=(datetime.fromisoformat(x.replace('Z','+00:00')) for x in (self.start,self.end))
        if a.tzinfo is None or b.tzinfo is None or not 0<(b-a).total_seconds()<=86400:raise ValueError('preview_requires_24h_timezone_window')
        self.start=a.astimezone(timezone.utc).isoformat();self.end=b.astimezone(timezone.utc).isoformat()
        if b>datetime.now(timezone.utc):raise ValueError('historical_window_required')
        return self

class ScheduleArtifacts(ArtifactService):
    def __init__(self,svc,slot):
        super().__init__(svc.runs,svc.cfg,svc.app.state.client);self.svc,self.slot=svc,slot
    async def fetch(self,path,params,jwt):
        expected='/api/v1/reports/energy' if self.slot['config']['task_type']=='daily_report' else '/api/v1/alarms'
        if path!=expected:raise ValueError('schedule_read_denied')
        kind='energy' if path.endswith('/energy') else 'alarms'
        r=await self.svc.bridge(self.slot,kind,{k:v for k,v in params.items() if k in ('page',)})
        if len(r.content)>32<<20:raise ValueError('schedule_read_limit')
        return r.content,dict(r.headers)

class ScheduleService:
    def __init__(self,app):self.app=app;self.runs=app.state.runs;self.pool=self.runs.pool;self.cfg=app.state.config;self.controls=set()
    def cross_database(self):
        from .production_profile import selected
        return bool(getattr(self.cfg,'v4_joint_profile','')) or selected()
    async def current_owner(self,jwt,owner):
        from .production_profile import selected
        if not selected():return
        await self.guard()
        r=await self.app.state.client.get(self.cfg.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.ops_token},timeout=3)
        if r.status_code!=200:raise HTTPException(403,'permission_changed')
        try:a=verify(r.json()['data']['context'],self.cfg.ma_actor_key,'ma-ops')
        except (ValueError,KeyError):raise HTTPException(403,'permission_changed') from None
        if a['user_id']!=owner or a['role'] not in ('operator','admin'):raise HTTPException(403,'permission_changed')
    async def guard(self):
        from .production_profile import selected as production_selected,guard as production_guard
        if production_selected():
            try:await production_guard(self.cfg,self.pool)
            except (ValueError,OSError,KeyError):raise HTTPException(403,'production_schedule_policy_required') from None
            return
        from .joint_profile import selected, guard
        if selected(self.cfg):
            try:await guard(self.cfg,self.pool,'rag')
            except ValueError:raise HTTPException(403,'isolated_s09_policy_required') from None
            return
        c=self.cfg;db=urlsplit(c.database_url)
        if not getattr(c,'v4_schedules_enabled',False) or not c.v4_runs_enabled or not c.v4_assistant_enabled:raise HTTPException(503,'schedules_disabled')
        if db.hostname!='127.0.0.1' or db.port!=25438 or not db.path.startswith('/v4_s08_') or not db.path.endswith('_test') or c.backend_url!='http://127.0.0.1:28982' or c.ops_url!='http://127.0.0.1:28983' or c.v4_actions_enabled or c.v4_ops_enabled or not c.v4_artifacts_enabled:raise HTTPException(403,'isolated_s08_policy_required')
    async def event(self,c,sid,owner,kind,detail=None):
        await c.execute('INSERT INTO assistant_schedule_events(schedule_id,user_id,kind,detail) VALUES($1,$2,$3,$4::jsonb)',sid,owner,kind,encode(detail or {}))
    async def get(self,sid,owner,c=None):
        row=await (c or self.pool).fetchrow('SELECT * FROM assistant_schedules WHERE id=$1 AND user_id=$2',sid,owner)
        if not row:raise HTTPException(404,'schedule_not_found')
        value=dict(row);value['config']=json.loads(value['config']);value.pop('auth_sealed',None)
        g=await (c or self.pool).fetchrow('SELECT grant_id,scope_hash,destination_id,destination_version,config_version,approved_by,approved_at,expires_at,revoked_at FROM assistant_schedule_grants WHERE schedule_id=$1 ORDER BY approved_at DESC LIMIT 1',sid)
        value['grant']=dict(g) if g else None
        return value
    async def bridge(self,slot,kind,params=None):
        await self.guard()
        r=await self.app.state.client.get(self.cfg.backend_url+'/internal/agent/schedule-read/'+str(slot['schedule_id']),params=dict(grant_id=str(slot['grant_id']),scheduled_for=slot.get('scheduled_for',datetime.now(timezone.utc)).isoformat(),kind=kind,**(params or {})),headers={'X-PowerSystem-Token':self.cfg.service_token},timeout=20)
        if r.status_code!=200:raise HTTPException(403 if r.status_code in (401,403,404) else 503,'schedule_permission_or_dependency_changed')
        return r
    async def valid(self,sid,gid,c=None,lock=False):
        db=c or self.pool
        s=await db.fetchrow('SELECT * FROM assistant_schedules WHERE id=$1'+(' FOR SHARE' if lock else ''),sid)
        g=await db.fetchrow('SELECT * FROM assistant_schedule_grants WHERE grant_id=$1'+(' FOR SHARE' if lock else ''),gid)
        if not s or not g or s['status']!='active' or g['schedule_id']!=sid or g['user_id']!=s['user_id'] or g['approved_by']!=s['user_id'] or g['revoked_at'] or g['expires_at']<=datetime.now(timezone.utc) or g['config_version']!=s['config_version'] or g['scope_hash']!=s['scope_hash'] or g['destination_id']!='platform_client' or g['destination_version']!='1':raise HTTPException(409,'schedule_grant_inactive')
        config=ScheduleConfig.model_validate_json(s['config']).model_dump()
        if config!=json.loads(s['config']) or digest(config)!=s['scope_hash']:raise HTTPException(409,'schedule_configuration_drift')
        if lock and not self.cross_database():
            role=await db.fetchval('SELECT role FROM users WHERE id=$1 FOR SHARE',s['user_id'])
            if role!=s['actor_role'] or role not in ('operator','admin'):raise HTTPException(403,'permission_changed')
        return {**dict(s),'config':config,'grant_id':gid}
    async def reduce(self,sid,owner,cancel=False,reason=None):
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow('SELECT * FROM assistant_schedules WHERE id=$1 AND user_id=$2 FOR UPDATE',sid,owner)
            if not row:raise HTTPException(404,'schedule_not_found')
            state='cancelled' if cancel or row['status']=='cancelled' else 'paused';reason=reason or ('owner_cancelled' if cancel else 'owner_paused')
            if row['status']!=state or reason!=row['reason']:
                await c.execute('UPDATE assistant_schedules SET status=$2,next_run_at=NULL,reason=$3,updated_at=now() WHERE id=$1',sid,state,reason)
                await c.execute('UPDATE assistant_schedule_grants SET revoked_at=coalesce(revoked_at,now()) WHERE schedule_id=$1',sid)
                await self.event(c,sid,owner,state,dict(reason=reason))
        # Cancel requests after releasing schedule lock. A published historical
        # artifact remains a fact; no new delivery can pass the grant lock.
        async def cancel_read(rid):
            try:await self.runs.cancel(rid,owner)
            except Exception:pass  # The revoked grant already fences publication.
        for row in await self.pool.fetch("SELECT r.id FROM assistant_schedule_slots t JOIN assistant_runs r ON r.id=t.run_id WHERE t.schedule_id=$1 AND r.status NOT IN ('succeeded','partial','failed','cancelled')",sid):
            task=asyncio.create_task(cancel_read(row['id']));self.controls.add(task);task.add_done_callback(self.controls.discard)
        return await self.get(sid,owner)
    async def snapshot(self,cap,args,owner,c=None):
        db=c or self.pool;sid=UUID(args['schedule_id']);version=args['expected_version']
        row=await db.fetchrow('SELECT * FROM assistant_schedules WHERE id=$1',sid)
        if cap=='schedule.create':
            if version!=0 or row:raise HTTPException(409,'schedule_version_changed')
            material=dict(schedule_id=str(sid),owner=owner,config_version=0,state='absent')
        else:
            if not row or row['user_id']!=owner:raise HTTPException(404,'schedule_not_found')
            if row['status']=='cancelled' or row['config_version']!=version:raise HTTPException(409,'schedule_version_changed')
            if cap=='schedule.resume' and (row['status']!='paused' or json.loads(row['config'])!=args['config']):raise HTTPException(409,'resume_configuration_changed')
            material=dict(schedule_id=str(sid),owner=owner,config_version=version,state=row['status'],scope_hash=row['scope_hash'],updated_at=row['updated_at'].isoformat())
        return dict(target_version=digest(material),target_summary='本人定期任务 '+str(sid))
    async def admit(self,owner,key,cap,args,actor,jwt,kind='config'):
        await self.guard()
        await self.current_owner(jwt,owner)
        message=('配置本人定期任务：' if kind=='config' else '只读规则历史预演：')+cap+' '+encode(args)
        async def admitted(c,rid):await c.execute('INSERT INTO assistant_schedule_requests(run_id,kind,capability,args) VALUES($1,$2,$3,$4::jsonb)',rid,kind,cap,encode(args))
        return await self.runs.accept(owner,key,message,None,None,run_profile(self.cfg),AuthVault(self.cfg.service_token).seal(owner,dict(jwt=jwt,actor=actor)),120,request_hash=digest(dict(kind=kind,capability=cap,args=args)),actor=actor,on_accept=admitted)
    async def apply(self,g,jwt):
        # Execute-config grants are short-lived, stage9-signed and one-shot.
        # The resulting persistent grant has no action/operation credential.
        await self.guard();owner=g['owner_id'];sid=UUID(g['typed_args']['schedule_id'])
        identity=await self.app.state.client.get(self.cfg.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.ops_token},timeout=3)
        if identity.status_code!=200:raise HTTPException(403,'permission_changed')
        current_actor=verify(identity.json()['data']['context'],self.cfg.ma_actor_key,'ma-ops')
        if current_actor['user_id']!=owner:raise HTTPException(403,'permission_changed')
        old=await self.pool.fetchrow('SELECT receipt FROM assistant_schedule_receipts WHERE execution_ref=$1 AND user_id=$2',UUID(g['execution_ref']),owner)
        if old:return json.loads(old['receipt'])
        response=await self.app.state.client.post(self.cfg.ops_url+'/internal/ma-schedule/v1/claims',json=dict(grant=g['_token']),headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.ops_token},timeout=8)
        if response.status_code!=200:raise HTTPException(response.status_code if response.status_code in (401,403,409) else 503,'schedule_claim_unconfirmed')
        claimed=response.json()
        args=g['typed_args'];config=ScheduleConfig.model_validate(args['config']).model_dump()
        try:
            async with self.pool.acquire() as c,c.transaction():
                await c.execute('SELECT pg_advisory_xact_lock($1)',int.from_bytes(sid.bytes[:8],'big',signed=True))
                if datetime.now(timezone.utc).timestamp()>=g['exp']:raise HTTPException(409,'schedule_dispatch_expired')
                old=await c.fetchrow('SELECT receipt FROM assistant_schedule_receipts WHERE execution_ref=$1',UUID(g['execution_ref']))
                if old:return json.loads(old['receipt'])
                row=await c.fetchrow('SELECT * FROM assistant_schedules WHERE id=$1 FOR UPDATE',sid)
                snap=await self.snapshot(g['capability'],args,owner,c)
                if snap['target_version']!=g['target_version']:raise HTTPException(409,'schedule_version_changed')
                # Stage9 already rechecked identity at claim. Recheck again inside
                # the config transaction using the actual backend current account.
                r=await self.app.state.client.get(self.cfg.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt,'X-PowerSystem-Token':self.cfg.ops_token},timeout=3)
                if r.status_code!=200:raise HTTPException(403,'permission_changed')
                actor=verify(r.json()['data']['context'],self.cfg.ma_actor_key,'ma-ops')
                if actor['user_id']!=owner or actor['role']!=claimed['actor_role'] or actor['role'] not in ('operator','admin'):raise HTTPException(403,'permission_changed')
                if not self.cross_database():
                    role=await c.fetchval('SELECT role FROM users WHERE id=$1 FOR SHARE',owner)
                    if role!=actor['role']:raise HTTPException(403,'permission_changed')
                at=datetime.now(timezone.utc);version=args['expected_version']+1;scope=digest(config);gid=uuid5(UUID(g['execution_ref']),'schedule-read-grant')
                if row:
                    if actor['role']!=row['actor_role']:raise HTTPException(403,'permission_changed')
                    await c.execute('UPDATE assistant_schedule_grants SET revoked_at=coalesce(revoked_at,now()) WHERE schedule_id=$1',sid)
                    await c.execute("UPDATE assistant_schedules SET config=$2::jsonb,config_version=$3,scope_hash=$4,status='active',next_run_at=$5,reason=NULL,updated_at=now() WHERE id=$1",sid,encode(config),version,scope,next_slot(config,at))
                else:
                    await c.execute("INSERT INTO assistant_schedules(id,user_id,config_version,config,scope_hash,actor_role,status,next_run_at,auth_sealed) VALUES($1,$2,$3,$4::jsonb,$5,$6,'active',$7,''::bytea)",sid,owner,version,encode(config),scope,actor['role'],next_slot(config,at))
                expiry=at+timedelta(days=config['grant_days'])
                await c.execute("INSERT INTO assistant_schedule_grants(grant_id,schedule_id,user_id,config_version,scope_hash,destination_id,destination_version,approved_by,approved_at,expires_at,action_id,execution_ref) VALUES($1,$2,$3,$4,$5,'platform_client','1',$3,$6,$7,$8,$9)",gid,sid,owner,version,scope,at,expiry,UUID(g['action_id']),UUID(g['execution_ref']))
                receipt=dict(execution_ref=g['execution_ref'],action_id=g['action_id'],run_id=g['run_id'],schedule_id=str(sid),config_version=version,grant_id=str(gid),expires_at=expiry.isoformat(),no_effect=False,effect_summary='本人只读定期任务配置已生效；授权至 '+expiry.astimezone(ZoneInfo(config['timezone'])).isoformat()+'，到期暂停，不自动续批。')
                await c.execute('INSERT INTO assistant_schedule_receipts(execution_ref,action_id,user_id,receipt) VALUES($1,$2,$3,$4::jsonb)',UUID(g['execution_ref']),UUID(g['action_id']),owner,encode(receipt))
                await self.event(c,sid,owner,g['capability'],dict(config_version=version,grant_id=str(gid),expires_at=expiry.isoformat(),execution_ref=g['execution_ref']))
        except HTTPException as error:
            # The local PostgreSQL transaction has rolled back. Persist that
            # negative fact so lost transport responses need only a receipt read.
            receipt=dict(execution_ref=g['execution_ref'],action_id=g['action_id'],run_id=g['run_id'],status='failed',no_effect=True,error_code=error.detail,effect_summary='配置事务已拒绝并回滚，未修改任务或授权；请按当前状态重新提议。')
            await self.pool.execute('INSERT INTO assistant_schedule_receipts(execution_ref,action_id,user_id,receipt) VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING',UUID(g['execution_ref']),UUID(g['action_id']),owner,encode(receipt))
        return receipt
    async def loop(self):
        while True:
            if getattr(self.cfg,'v4_schedules_enabled',False):
                try:await self.tick()
                except asyncio.CancelledError:raise
                except Exception as error:
                    try:await self.event(self.pool,None,0,'scheduler_error',dict(error_type=type(error).__name__))
                    except Exception:pass  # DB outage must not kill the scheduler loop.
            await asyncio.sleep(1)
    async def tick(self,now=None):
        await self.guard();now=now or datetime.now(timezone.utc)
        # Expiry is independent of the next due slot.
        invalid=await self.pool.fetch("SELECT s.id,s.user_id FROM assistant_schedules s WHERE s.status='active' AND NOT EXISTS(SELECT 1 FROM assistant_schedule_grants g WHERE g.schedule_id=s.id AND g.revoked_at IS NULL AND g.expires_at>$1)",now)
        for row in invalid:await self.reduce(row['id'],row['user_id'],reason='grant_expired_or_revoked')
        for row in await self.pool.fetch("SELECT s.id,s.user_id,g.grant_id FROM assistant_schedules s JOIN assistant_schedule_grants g ON g.schedule_id=s.id WHERE s.status='active' AND g.revoked_at IS NULL"):
            try:
                item=await self.valid(row['id'],row['grant_id'])
                await self.bridge({**item,'schedule_id':row['id']},'check')
            except Exception:await self.reduce(row['id'],row['user_id'],reason='grant_permission_or_configuration_changed')
        for _ in range(20):
            async with self.pool.acquire() as c,c.transaction():
                row=await c.fetchrow("SELECT * FROM assistant_schedules WHERE status='active' AND next_run_at<=$1 ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT 1",now)
                if not row:return
                g=await c.fetchrow('SELECT * FROM assistant_schedule_grants WHERE schedule_id=$1 AND revoked_at IS NULL',row['id'])
                try:
                    if not g:raise ValueError()
                    slot=await self.valid(row['id'],g['grant_id'],c)
                    actor=(await self.bridge({**slot,'schedule_id':row['id']},'check')).json()
                    if actor['user_id']!=row['user_id'] or actor['role']!=row['actor_role']:raise ValueError()
                except Exception:
                    await c.execute("UPDATE assistant_schedules SET status='paused',reason='grant_permission_or_configuration_changed',next_run_at=NULL WHERE id=$1",row['id'])
                    await c.execute('UPDATE assistant_schedule_grants SET revoked_at=coalesce(revoked_at,now()) WHERE schedule_id=$1',row['id'])
                    await self.event(c,row['id'],row['user_id'],'paused',dict(reason='grant_permission_or_configuration_changed'));continue
                due=row['next_run_at'];missed=(now-due).total_seconds()>60
                count=missed_count(slot['config'],due,now) if missed else 0
                saved=await c.fetchval("INSERT INTO assistant_schedule_slots(schedule_id,scheduled_for,grant_id,config_version,status,reason,skipped_count) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING RETURNING schedule_id",row['id'],due,g['grant_id'],row['config_version'],'skipped' if missed else 'claimed','missed_slot_no_catchup' if missed else None,count)
                await c.execute('UPDATE assistant_schedules SET next_run_at=$2,updated_at=now() WHERE id=$1',row['id'],next_slot(slot['config'],now))
                if not saved:continue
                if missed:
                    await self.event(c,row['id'],row['user_id'],'skipped',dict(first_slot=due.isoformat(),observed_at=now.isoformat(),skipped_count=count));continue
                key=uuid5(row['id'],due.isoformat())
                async def admitted(conn,rid):await conn.execute('UPDATE assistant_schedule_slots SET run_id=$3 WHERE schedule_id=$1 AND scheduled_for=$2',row['id'],due,rid)
                receipt=await self.runs.accept(row['user_id'],key,'定期'+('日报' if slot['config']['task_type']=='daily_report' else '巡检')+' · '+due.isoformat(),None,None,run_profile(self.cfg),AuthVault(self.cfg.service_token).seal(row['user_id'],dict(jwt='',actor=actor)),120,actor=actor,on_accept=admitted,connection=c)
                await self.event(c,row['id'],row['user_id'],'triggered',dict(scheduled_for=due.isoformat(),grant_id=str(g['grant_id']),run_id=receipt['run_id']))
    async def run(self,runtime,auth,result,request=None,slot=None):
        row=runtime.row
        if request and request['kind']=='config':
            aid=uuid5(row['id'],'schedule-config-action');args=json.loads(request['args'])
            body=dict(action_id=str(aid),run_id=str(row['id']),session_id=str(row['session_id']),parent_turn_id=row['turn_id'],capability=request['capability'],typed_args=args,user_request=row['request']['message'])
            value=await runtime.ops.request('POST','/internal/ma-schedule/v1/actions',body,body)
            await self.runs.project_authority(row['id'],row['user_id'],[value]);result['_run_action_pause']=True
            return []
        if request:return await self.preview(runtime,auth,result,Preview.model_validate_json(request['args']))
        gid=slot['grant_id'];sid=slot['schedule_id'];slot={**dict(slot),**await self.valid(sid,gid),'schedule_id':sid}
        await self.bridge(slot,'check')
        original=runtime.check
        async def check():
            await original();await self.valid(sid,gid);await self.bridge(slot,'check')
        async def delivery(c):
            await self.valid(sid,gid,c,True);await self.bridge(slot,'check')
        runtime.check=check;runtime.authorize_delivery=delivery
        config=slot['config'];due=slot['scheduled_for'];filters={k:config[k] for k in ('scope','station_code','device_id')}
        if config['task_type']=='daily_report':
            day=due.astimezone(ZoneInfo('Asia/Shanghai')).date()
            args=ReportArgs(kind='energy',start_date=str(day-timedelta(days=1)),end_date=str(day),**filters)
        else:args=ReportArgs(kind='investigation',start=(due-timedelta(hours=24)).isoformat(),end=due.isoformat(),**filters)
        service=ScheduleArtifacts(self,slot)
        value=await runtime.journal('schedule-report','read_tool',dict(config_version=slot['config_version'],grant_id=str(gid),slot=due.isoformat()),lambda:service.create(runtime,'schedule-delivery',args,''),result)
        await check()
        result.update(status='answered',artifacts=value['artifacts'],answer_markdown=('定期日报' if config['task_type']=='daily_report' else '定期巡检调查清单')+'已交付本站本人收件箱。\n\n'+('北京时间上一完整自然日发电量；缺测保持未知。' if config['task_type']=='daily_report' else '时槽前24小时告警只读观察；待核查项目与缺测见清单，不代表现场检查或故障修复。')+'\n\n时槽：'+due.isoformat()+'；范围：'+encode(filters)+'；配置版本：'+str(slot['config_version'])+'。')
        return []
    async def preview(self,runtime,auth,result,args):
        from .rule_preview import evaluate
        async def collect():
            raw,_=await self.app.state.artifacts.fetch('/api/v1/devices/'+str(args.device_id)+'/telemetry',dict(metric=args.metric,start=args.start,end=args.end),auth['jwt'])
            envelope=json.loads(raw)
            if envelope.get('code')!=0:raise ValueError('history_unavailable')
            return envelope['data']
        snapshot=await runtime.journal('rule-history-snapshot','read_tool',args.model_dump(),collect,result)
        value=evaluate(args.model_dump(),snapshot)
        public=await publish_json(self.app.state.artifacts,runtime,'rule-preview',value)
        result.update(status='answered',artifacts=public,answer_markdown='只读历史预演已完成。\n\n'+value['summary']+'\n\n这是历史采样上的阈值命中比较；未修改线上规则，没有现场真值，不能证明漏报率或真实收益。范围、缺测、快照摘要与假设见本站本人产物。')
        return []

async def publish_json(service,runtime,call,value):
    import os
    rid=runtime.row['id'];aid=uuid5(rid,call)
    old=await service.pool.fetchrow('SELECT *,id AS artifact_id FROM assistant_artifacts WHERE id=$1',aid)
    if old:return [metadata(old)]
    raw=encode(value).encode();key=str(uuid4());service.root.mkdir(parents=True,exist_ok=True);target=service.path(key)
    if len(raw)>32<<20:raise ValueError('preview_limit')
    temp=target.with_suffix('.'+str(runtime.worker)+'.tmp')
    try:
        temp.write_bytes(raw);os.replace(temp,target)
        async with service.pool.acquire() as c,c.transaction():
            row=await service.runs.locked(c,rid,runtime.worker,runtime.row['lease_epoch'])
            if row['cancel_requested']:raise ValueError('preview_cancelled')
            saved=await c.fetchrow("INSERT INTO assistant_artifacts(id,run_id,user_id,call_id,name,mime_type,storage_key,size,sha256,status,provenance) VALUES($1,$2,$3,$4,'rule-preview.json','application/json',$5,$6,$7,'ready',$8::jsonb) RETURNING *,id AS artifact_id",aid,rid,row['user_id'],call,key,len(raw),hashlib.sha256(raw).hexdigest(),encode(dict(mode='simulation',source_hash=value['snapshot_sha256'],model_content=False)))
            item=metadata(saved);row['artifacts'].append(item);await service.runs._event(c,row,'artifact_ready',item)
        return [item]
    except BaseException:
        if not await service.pool.fetchval('SELECT EXISTS(SELECT 1 FROM assistant_artifacts WHERE storage_key=$1)',key):target.unlink(missing_ok=True)
        raise
    finally:temp.unlink(missing_ok=True)

def routes(app):
    def svc():return app.state.schedules
    def jwt(req):
        v=req.headers.get('Authorization','')
        if not v.startswith('Bearer ') or not v[7:]:raise HTTPException(401,'missing_user_token')
        return v[7:]
    def actor(req,owner):
        try:
            a=verify(req.headers.get('X-MA-Actor',''),app.state.config.ma_actor_key,'ma-ops')
            if a['user_id']!=owner or a['role'] not in ('operator','admin'):raise ValueError()
            return {k:a[k] for k in ('user_id','role')}
        except (ValueError,KeyError):raise HTTPException(403,'invalid_actor') from None
    def key(req):
        try:return UUID(req.headers.get('Idempotency-Key',''))
        except ValueError:raise HTTPException(400,'invalid_idempotency_key') from None
    @app.get('/internal/agent/schedules')
    async def listing(user_id:int,request:Request,offset:int=0):
        jwt(request)
        await svc().current_owner(jwt(request),user_id)
        if offset<0:raise HTTPException(400,'invalid_offset')
        rows=await svc().pool.fetch('SELECT id FROM assistant_schedules WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50 OFFSET $2',user_id,offset)
        recent=await svc().pool.fetch('SELECT r.id AS run_id,r.turn_id,r.session_id,r.status,q.capability FROM assistant_schedule_requests q JOIN assistant_runs r ON r.id=q.run_id WHERE r.user_id=$1 ORDER BY r.created_at DESC LIMIT 30',user_id)
        return dict(list=[await svc().get(r['id'],user_id) for r in rows],offset=offset,runs=[dict(r) for r in recent])
    @app.get('/internal/agent/schedules/{sid}')
    async def get(sid:UUID,user_id:int,request:Request):
        await svc().current_owner(jwt(request),user_id)
        return await svc().get(sid,user_id)
    @app.get('/internal/agent/schedules/{sid}/history')
    async def history(sid:UUID,user_id:int,request:Request,offset:int=0):
        jwt(request);await svc().get(sid,user_id)
        await svc().current_owner(jwt(request),user_id)
        if offset<0:raise HTTPException(400,'invalid_offset')
        rows=await svc().pool.fetch('SELECT t.*,r.status AS run_status,r.turn_id,r.session_id,r.final_answer,r.artifacts FROM assistant_schedule_slots t LEFT JOIN assistant_runs r ON r.id=t.run_id WHERE t.schedule_id=$1 ORDER BY scheduled_for DESC LIMIT 50 OFFSET $2',sid,offset)
        return dict(list=[{**dict(r),'artifacts':json.loads(r['artifacts']) if r['artifacts'] else []} for r in rows],offset=offset)
    @app.post('/internal/agent/schedules',status_code=202)
    async def create(body:Create,user_id:int,request:Request):
        k=key(request);sid=uuid5(NAMESPACE_URL,'powersystem:s08:'+str(user_id)+':'+str(k));a=actor(request,user_id)
        receipt=await svc().admit(user_id,k,'schedule.create',dict(schedule_id=str(sid),expected_version=0,config=body.config.model_dump()),a,jwt(request))
        return dict(**receipt,schedule_id=str(sid))
    @app.put('/internal/agent/schedules/{sid}',status_code=202)
    async def update(sid:UUID,body:Update,user_id:int,request:Request):
        await svc().get(sid,user_id)
        return await svc().admit(user_id,key(request),'schedule.update',dict(schedule_id=str(sid),expected_version=body.expected_version,config=body.config.model_dump()),actor(request,user_id),jwt(request))
    @app.post('/internal/agent/schedules/{sid}/resume',status_code=202)
    async def resume(sid:UUID,body:Resume,user_id:int,request:Request):
        row=await svc().get(sid,user_id)
        return await svc().admit(user_id,key(request),'schedule.resume',dict(schedule_id=str(sid),expected_version=body.expected_version,config=row['config']),actor(request,user_id),jwt(request))
    @app.post('/internal/agent/schedules/{sid}/pause')
    async def pause(sid:UUID,body:Empty,user_id:int,request:Request):
        await svc().current_owner(jwt(request),user_id)
        return await svc().reduce(sid,user_id)
    @app.post('/internal/agent/schedules/{sid}/cancel')
    async def cancel(sid:UUID,body:Empty,user_id:int,request:Request):
        await svc().current_owner(jwt(request),user_id)
        return await svc().reduce(sid,user_id,True)
    @app.post('/internal/agent/rule-preview',status_code=202)
    async def preview(body:Preview,user_id:int,request:Request):return await svc().admit(user_id,key(request),'rule.preview',body.model_dump(),actor(request,user_id),jwt(request),'preview')
    @app.post('/internal/ma-schedule/v1/snapshot')
    async def snapshot(request:Request):
        await svc().guard();raw=await request.json()
        if set(raw)!={'capability','typed_args'} or raw['capability'] not in ('schedule.create','schedule.update','schedule.resume'):raise HTTPException(400,'invalid_snapshot')
        # The gateway remains the identity source; this endpoint is service-only.
        r=await app.state.client.get(app.state.config.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt(request),'X-PowerSystem-Token':app.state.config.ops_token},timeout=3)
        if r.status_code!=200:raise HTTPException(403,'permission_changed')
        a=verify(r.json()['data']['context'],app.state.config.ma_actor_key,'ma-ops')
        return await svc().snapshot(raw['capability'],raw['typed_args'],a['user_id'])
    @app.post('/internal/ma-schedule/v1/execute')
    async def execute(request:Request):
        # Domain verification is locally defined: no stage9 package dependency.
        from .schedule_token import verify_action
        raw=await request.json()
        if not isinstance(raw,dict) or set(raw)!={'grant'}:raise HTTPException(400,'invalid_grant')
        from .production_profile import selected,SCHEDULE_POLICY
        try:g=verify_action(raw['grant'],app.state.config.ma_actor_key,expected_policy=SCHEDULE_POLICY if selected() else POLICY)
        except ValueError:raise HTTPException(403,'invalid_grant') from None
        return await svc().apply({**g,'_token':raw['grant']},jwt(request))
    @app.get('/internal/ma-schedule/v1/receipts/{ref}')
    async def receipt(ref:UUID,request:Request):
        r=await app.state.client.get(app.state.config.backend_url+'/internal/ma/v1/actor',headers={'Authorization':'Bearer '+jwt(request),'X-PowerSystem-Token':app.state.config.ops_token},timeout=3)
        if r.status_code!=200:raise HTTPException(403,'permission_changed')
        a=verify(r.json()['data']['context'],app.state.config.ma_actor_key,'ma-ops')
        value=await svc().pool.fetchval('SELECT receipt FROM assistant_schedule_receipts WHERE execution_ref=$1 AND user_id=$2',ref,a['user_id'])
        if not value:raise HTTPException(404,'receipt_not_found')
        return json.loads(value)
