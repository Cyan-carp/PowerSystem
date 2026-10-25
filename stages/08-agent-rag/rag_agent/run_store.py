"""PostgreSQL is the admission, event and lease authority. No Redis dependency."""
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import HTTPException
from .ma_security import digest

PROTOCOL = 'assistant/v4-draft1'
TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled'}
EVENT_TERMINAL = {'succeeded': 'completed', 'partial': 'completed', 'failed': 'failed', 'cancelled': 'cancelled'}


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def unpack(row):
    value = dict(row)
    for key in ('request','context_snapshot','model_profile','result_refs','actions','artifacts','terminal_event','error','auth_actor'):
        if isinstance(value.get(key), str): value[key] = json.loads(value[key])
    return value


class RunStore:
    def __init__(self, store):
        self.store, self.pool = store, store.pool

    @asynccontextmanager
    async def connection(self, existing=None):
        if existing is not None:yield existing
        else:
            async with self.pool.acquire() as c:yield c

    async def accept(self, user, key, message, session, page, profile, sealed, seconds, request_hash=None,actor=None,on_accept=None,connection=None):
        material = dict(protocol=PROTOCOL, message=message, session_id=str(session) if session else None, page_context=page)
        hashed = request_hash or digest(material)
        async with self.connection(connection) as c, c.transaction():
            # Serialize duplicate keys before making a new session. The SQL unique constraint is still mandatory.
            await c.execute('SELECT pg_advisory_xact_lock($1)', int.from_bytes(hashlib.sha256(f'{user}:{key}'.encode()).digest()[:8], 'big', signed=True))
            row = await c.fetchrow('SELECT * FROM assistant_runs WHERE user_id=$1 AND idempotency_key=$2', user, key)
            if row:
                if row['request_hash'] != hashed: raise HTTPException(409, 'idempotency_conflict')
                return self.receipt(row)
            sid = session or uuid4()
            if session:
                owner = await c.fetchval('SELECT user_id FROM rag_sessions WHERE id=$1 AND deleted_at IS NULL FOR UPDATE', sid)
                if owner != user: raise HTTPException(404, 'session_not_found')
            else:
                await c.execute('INSERT INTO rag_sessions(id,user_id,title) VALUES($1,$2,$3)', sid, user, message[:100])
            captured=await c.fetchrow('''SELECT
              (SELECT coalesce(jsonb_agg(t ORDER BY id),'[]') FROM (SELECT id,question,response,model_messages,kind,parent_turn_id,run_id,created_at FROM rag_turns WHERE session_id=$1) t) AS turns,
              (SELECT coalesce(jsonb_agg(r ORDER BY created_at),'[]') FROM (SELECT id,status,last_seq,answer_version,result_refs,updated_at,created_at FROM assistant_runs WHERE session_id=$1) r) AS runs''',sid)
            rows=json.loads(captured['turns'])
            visible_runs=json.loads(captured['runs'])
            by_id={r['id']:r for r in visible_runs}
            previous = []
            for r in rows:
                t = dict(r)
                if t.get('run_id') in by_id:
                    t['_run_snapshot']=by_id[t['run_id']]
                    if not t['response'].get('task_cards'):
                        t['response']['task_cards']=[{**ref,'historical':True} for ref in t['_run_snapshot']['result_refs']]
                previous.append(t)
            # Turn state and associated run projection are locked at this transaction's visible prefix.
            snapshot = dict(turns=previous, runs=visible_runs, page_context=page, captured_at=datetime.now(timezone.utc).isoformat())
            rid = uuid4()
            response = dict(status='queued', answer_markdown='', task_cards=[], run_id=str(rid))
            turn = await c.fetchval('INSERT INTO rag_turns(session_id,question,response,model_messages) VALUES($1,$2,$3::jsonb,\'[]\') RETURNING id',sid,message,encode(response))
            await c.execute('''INSERT INTO assistant_runs(id,user_id,session_id,turn_id,idempotency_key,request_hash,request,context_snapshot,model_profile,auth_sealed,deadline)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8::jsonb,$9::jsonb,$10,now()+$11*interval '1 second')''',rid,user,sid,turn,key,hashed,encode(material),encode(snapshot),encode(profile),sealed,float(seconds))
            await c.execute('UPDATE rag_turns SET run_id=$2 WHERE id=$1',turn,rid)
            await c.execute('UPDATE assistant_runs SET auth_actor=$2::jsonb WHERE id=$1',rid,encode(actor))
            if on_accept:await on_accept(c,rid)
            await c.execute('INSERT INTO assistant_run_outbox(run_id) VALUES($1)',rid)
            row = unpack(await c.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 FOR UPDATE',rid))
            await self._event(c,row,'accepted',dict(status='queued',session_id=str(sid),turn_id=turn))
            await c.execute('UPDATE rag_sessions SET updated_at=now() WHERE id=$1',sid)
            return self.receipt(row)

    @staticmethod
    def receipt(row):
        return dict(run_id=str(row['id']),turn_id=row['turn_id'],session_id=str(row['session_id']),status=row['status'])

    async def _event(self,c,row,kind,data):
        seq=row['last_seq']+1
        now=datetime.now(timezone.utc)
        event=dict(protocol=PROTOCOL,run_id=str(row['id']),turn_id=row['turn_id'],seq=seq,type=kind,at=now.isoformat(),data=data)
        await c.execute('INSERT INTO assistant_run_events(run_id,seq,type,at,data) VALUES($1,$2,$3,$4,$5::jsonb)',row['id'],seq,kind,now,encode(data))
        row['last_seq']=seq
        if kind in ('completed','failed','cancelled'): row['terminal_event']=event
        await c.execute('''UPDATE assistant_runs SET status=$2,phase=$3,final_answer=$4,answer_version=$5,result_refs=$6::jsonb,
            actions=$7::jsonb,artifacts=$8::jsonb,terminal_event=$9::jsonb,last_seq=$10,error=$11::jsonb,
            cancel_requested=$12,requires_manual_reconciliation=$13,updated_at=now() WHERE id=$1''',row['id'],row['status'],row['phase'],row['final_answer'],row['answer_version'],encode(row['result_refs']),encode(row['actions']),encode(row['artifacts']),encode(row['terminal_event']),seq,encode(row['error']),row['cancel_requested'],row['requires_manual_reconciliation'])
        return event

    async def snapshot(self,rid,user):
        # All projected fields and last_seq live in one row, read under one MVCC statement.
        row=await self.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 AND user_id=$2',rid,user)
        if not row: raise HTTPException(404,'run_not_found')
        row=unpack(row)
        keys=('session_id','turn_id','status','phase','cancel_requested','requires_manual_reconciliation','final_answer','answer_version','result_refs','actions','artifacts','terminal_event','last_seq','error')
        result={k:row[k] for k in keys}
        result.update(protocol=PROTOCOL,run_id=str(rid),session_id=str(row['session_id']))
        # A projection is never presented as a current authority read.
        result['actions']=[{**a,'stale':a.get('stale',True)} for a in result['actions']]
        return result

    async def event_batch(self,rid,user,after):
        async with self.pool.acquire() as c, c.transaction(isolation='repeatable_read',readonly=True):
            row=await c.fetchrow('SELECT last_seq,status,turn_id FROM assistant_runs WHERE id=$1 AND user_id=$2',rid,user)
            if not row: raise HTTPException(404,'run_not_found')
            if after>row['last_seq']: raise HTTPException(400,'event_cursor_future')
            earliest=await c.fetchval("SELECT min(seq) FROM assistant_run_events WHERE run_id=$1 AND at>now()-interval '7 days'",rid)
            if after<row['last_seq'] and (earliest is None or after<earliest-1): raise HTTPException(410,'event_cursor_expired')
            events=await c.fetch("SELECT seq,type,at,data FROM assistant_run_events WHERE run_id=$1 AND seq>$2 AND at>now()-interval '7 days' ORDER BY seq LIMIT 200",rid,after)
            return [dict(protocol=PROTOCOL,run_id=str(rid),turn_id=row['turn_id'],seq=e['seq'],type=e['type'],at=e['at'].isoformat(),data=json.loads(e['data'])) for e in events],row['status'] in TERMINAL

    async def claim(self,worker,lease=15):
        async with self.pool.acquire() as c,c.transaction():
            # Cross-worker owner concurrency serialization, without holding a global lock across work.
            await c.execute('SELECT pg_advisory_xact_lock(803001)')
            row=await c.fetchrow('''SELECT r.* FROM assistant_runs r JOIN assistant_run_outbox o ON o.run_id=r.id
              WHERE r.status IN ('queued','running','reconciling') AND (r.lease_until IS NULL OR r.lease_until<now())
              AND o.available_at<=now() AND (r.status<>'queued' OR
                (SELECT count(*) FROM assistant_runs a WHERE a.user_id=r.user_id AND a.id<>r.id AND a.status IN ('running','reconciling') AND a.lease_until>now())<2)
              ORDER BY r.created_at FOR UPDATE OF r SKIP LOCKED LIMIT 1''')
            if not row:return None
            row=unpack(row)
            await c.execute("UPDATE assistant_runs SET lease_owner=$2,lease_epoch=lease_epoch+1,lease_until=now()+$3*interval '1 second' WHERE id=$1",row['id'],worker,float(lease))
            row['lease_owner']=worker;row['lease_epoch']+=1
            if row['status']=='queued':
                row.update(status='running',phase='starting')
                await self._event(c,row,'progress',dict(phase='starting',message='任务开始处理'))
            await c.execute('UPDATE assistant_run_outbox SET delivered_at=now() WHERE run_id=$1',row['id'])
            return row

    async def renew(self,rid,worker,epoch,lease=15):
        return await self.pool.execute("UPDATE assistant_runs SET lease_until=now()+$4*interval '1 second' WHERE id=$1 AND lease_owner=$2 AND lease_epoch=$3 AND lease_until>now()",rid,worker,epoch,float(lease))=='UPDATE 1'

    async def locked(self,c,rid,worker,epoch):
        row=await c.fetchrow('SELECT *,lease_until>now() AS lease_valid FROM assistant_runs WHERE id=$1 FOR UPDATE',rid)
        if not row or row['lease_owner']!=worker or row['lease_epoch']!=epoch or not row['lease_valid']:raise LostLease()
        return unpack(row)

    async def emit(self,rid,worker,epoch,kind,data):
        async with self.pool.acquire() as c,c.transaction():
            row=await self.locked(c,rid,worker,epoch)
            if row['status'] in TERMINAL:return
            if kind=='progress' and data.get('phase'):row['phase']=data['phase']
            return await self._event(c,row,kind,data)

    async def finish(self,rid,worker,epoch,status,result,messages,error=None,manual=False):
        async with self.pool.acquire() as c,c.transaction():
            row=await self.locked(c,rid,worker,epoch)
            if row['status'] in TERMINAL:return
            if not row['cancel_requested'] and any(a.get('status') in ('pending','approved') for a in row['actions']):
                raise AwaitingAuthority()
            if any(a.get('status') in ('started','execution_uncertain','unknown') for a in row['actions']) and status!='partial':
                raise NeedsReconciliation()
            if row['cancel_requested'] and not row['actions']:status='cancelled'
            answer=result.get('answer_markdown','')
            if answer!=row['final_answer']:
                row.update(final_answer=answer,answer_version=row['answer_version']+1)
                await self._event(c,row,'text_delta',dict(answer_version=row['answer_version'],offset=0,text=answer,reset=True))
            row.update(status=status,phase='finished',error=error,requires_manual_reconciliation=manual,
                       result_refs=[dict(task_id=t['task_id'],status=t['status'],historical=bool(t.get('historical'))) for t in result.get('task_cards',[])] or row['result_refs'])
            terminal=dict(status=status,answer_version=row['answer_version'],result_refs=row['result_refs'],error=error,requires_manual_reconciliation=manual)
            await self._event(c,row,EVENT_TERMINAL[status],terminal)
            response={**{k:v for k,v in result.items() if not k.startswith('_run_')},'run_id':str(rid),'status':status,'error':error}
            await c.execute('UPDATE rag_turns SET response=$2::jsonb,model_messages=$3::jsonb WHERE id=$1',row['turn_id'],encode(response),encode(messages))
            await c.execute("UPDATE assistant_runs SET auth_sealed=CASE WHEN (SELECT cancel_children FROM assistant_run_outbox WHERE run_id=$1) THEN auth_sealed ELSE ''::bytea END,lease_until=NULL WHERE id=$1",rid)

    async def child(self,rid,worker,epoch,tid,status):
        async with self.pool.acquire() as c,c.transaction():
            row=await self.locked(c,rid,worker,epoch)
            row['result_refs']=[r for r in row['result_refs'] if r.get('task_id')!=str(tid)]+[dict(task_id=str(tid),status=status,historical=False)]
            await self._event(c,row,'progress',dict(phase='child_task',message='子任务已绑定本轮，正在核对实际进度'))

    async def enqueue_child_cancel(self,rid):
        await self.pool.execute("UPDATE assistant_run_outbox SET cancel_children=true,cancel_retry_at=now() WHERE run_id=$1",rid)

    async def cancel(self,rid,user):
        async with self.pool.acquire() as c,c.transaction():
            record=await c.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',rid,user)
            if not record:raise HTTPException(404,'run_not_found')
            row=unpack(record)
            if row['status'] not in TERMINAL:
                first=not row['cancel_requested'];row['cancel_requested']=True
                if row['actions']:
                    row.update(status='reconciling',phase='reconciling')
                    await self._event(c,row,'progress',dict(phase='reconciling',message='正在核对既有动作的权威状态'))
                elif row['status'] in ('queued','awaiting_approval'):
                    row.update(status='cancelled',phase='finished')
                    await self._event(c,row,'cancelled',dict(status='cancelled',answer_version=row['answer_version'],result_refs=row['result_refs'],error=None,requires_manual_reconciliation=False))
                    await c.execute("UPDATE rag_turns SET response=response||'{\"status\":\"cancelled\"}'::jsonb WHERE id=$1",row['turn_id'])
                    await c.execute("UPDATE assistant_runs SET auth_sealed=''::bytea WHERE id=$1",rid)
                elif first:
                    await self._event(c,row,'progress',dict(phase=row['phase'],message='已请求取消，正在核对实际状态'))
        return await self.snapshot(rid,user)

    async def project_authority(self,rid,user,actions):
        """Only called by a trusted controller adapter, never exposed as a public mutation route."""
        allowed={'action_id','task_id','approval_id','plan_hash','expires_at','risk_class','effect_summary','target_summary','authority_revision','observed_at','status','execution_ref','stale','approval_status','consumed_at','target_version','capability','typed_args','error_code','display_result','no_effect','protocol','policy_version','destination_id','recovery_limit','health_check','action_confirmed','parameters_summary'}
        async with self.pool.acquire() as c,c.transaction():
            record=await c.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 AND user_id=$2 FOR UPDATE',rid,user)
            if not record:raise HTTPException(404,'run_not_found')
            row=unpack(record)
            for source in actions:
                action={k:v for k,v in source.items() if k in allowed}
                aid=UUID(action['action_id'])
                old=await c.fetchrow('SELECT * FROM assistant_actions WHERE action_id=$1 FOR UPDATE',aid)
                if old:
                    if old['run_id']!=rid or old['user_id']!=user:raise HTTPException(409,'action_binding_conflict')
                    if old['authority_revision']>action['authority_revision']:continue
                    if old['authority_revision']==action['authority_revision']:
                        previous=json.loads(old['projection'])
                        if {k:v for k,v in previous.items() if k not in ('observed_at','stale')}=={k:v for k,v in action.items() if k not in ('observed_at','stale')}:continue
                await c.execute('''INSERT INTO assistant_actions(action_id,run_id,user_id,authority_revision,observed_at,projection)
                  VALUES($1,$2,$3,$4,$5,$6::jsonb) ON CONFLICT(action_id) DO UPDATE SET authority_revision=EXCLUDED.authority_revision,observed_at=EXCLUDED.observed_at,projection=EXCLUDED.projection''',aid,rid,user,action['authority_revision'],datetime.fromisoformat(action['observed_at'].replace('Z','+00:00')),encode(action))
            rows=await c.fetch('SELECT projection FROM assistant_actions WHERE run_id=$1 ORDER BY action_id',rid)
            latest=[json.loads(r['projection']) for r in rows]
            if latest==row['actions']:return
            row['actions']=latest
            if row['status'] not in TERMINAL:
                if any(a['status'] in ('started','execution_uncertain','unknown') for a in latest):row.update(status='reconciling',phase='reconciling')
                elif any(a['status']=='pending' for a in latest):row.update(status='awaiting_approval',phase='awaiting_approval')
                elif row['status']=='awaiting_approval':row.update(status='running',phase='authority_updated')
            pending=next((a for a in latest if a['status']=='pending'),None)
            blocked=next((a for a in latest if a['status']=='blocked'),None)
            if pending and row['status'] not in TERMINAL:
                await self._event(c,row,'approval_required',{k:pending[k] for k in ('action_id','approval_id','plan_hash','expires_at','risk_class','effect_summary','target_summary','authority_revision')})
            elif blocked:
                await self._event(c,row,'action_blocked',dict(action_id=blocked['action_id'],rule_codes=[blocked.get('error_code') or 'blocked'],reason=blocked['effect_summary']))
            else:await self._event(c,row,'progress',dict(phase=row['phase'],message='动作权威状态已核对'))


class LostLease(Exception):pass
class NeedsReconciliation(Exception):pass
class AwaitingAuthority(Exception):pass
