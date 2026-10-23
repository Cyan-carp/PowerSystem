import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import asyncpg
from fastapi import HTTPException
from .schemas import ACTIVE, TERMINAL, ResultEnvelope
from .security import digest
from .evidence import build_evidence
from ops_contract import validate_catalog, public_asset, TOOLS
from .approvals import ApprovalStore


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def display_intent(intent):
    try:
        value=json.loads(intent)
        if isinstance(value,dict) and 'verification_id' in value and type(value.get('device_id')) is int:
            names={'latest_telemetry':'最新遥测','current_alarms':'当前告警','prediction':'预测'}
            return '主动核验 · 设备 #'+str(value['device_id'])+' · '+'、'.join(names[k] for k in value['checks'])
    except (ValueError,TypeError,KeyError):pass
    return intent


class LostLease(Exception):
    pass


class Store(ApprovalStore):
    def __init__(self, pool, cfg):
        self.pool, self.cfg = pool, cfg
        self.security_tables = False
        self.security_gates = None
        self.security_required = False
        self.task_tombstone = False

    @classmethod
    async def open(cls, cfg):
        pool = await asyncpg.create_pool(cfg.database_url, min_size=1, max_size=8, command_timeout=10)
        async with pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(90900)")
            if cfg.runtime_profile != "m4-restore-readonly":
                await c.execute((Path(__file__).resolve().parents[1] / "deploy/001_tasks.sql").read_text(encoding="utf-8"))
                await c.execute((Path(__file__).resolve().parents[1] / 'deploy/002_assets.sql').read_text(encoding='utf-8'))
                await c.execute((Path(__file__).resolve().parents[1] / 'deploy/004_approvals.sql').read_text(encoding='utf-8'))
                await c.execute((Path(__file__).resolve().parents[1] / 'deploy/007_runtime_context.sql').read_text(encoding='utf-8'))
                await c.execute((Path(__file__).resolve().parents[1] / 'deploy/008_task_deletion.sql').read_text(encoding='utf-8'))
                if cfg.security_enabled:
                    for migration in ('005_security_reviews.sql', '006_security_gates.sql'):
                        await c.execute((Path(__file__).resolve().parents[1] / 'deploy' / migration).read_text(encoding='utf-8'))
            has_reviews = await c.fetchval("SELECT to_regclass('ma_security_reviews') IS NOT NULL")
            has_gates = await c.fetchval("SELECT to_regclass('ma_security_gate_contexts') IS NOT NULL")
            # Restore-readonly skips migrations and runs against older dumps, so the tombstone
            # column is probed at runtime instead of being assumed present. Resolve the table
            # through to_regclass so the probe reads the same relation the queries will use;
            # information_schema.columns matches on table name across every schema.
            has_tombstone = await c.fetchval("SELECT EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid=to_regclass('ma_tasks') AND attname='deleted_at' AND NOT attisdropped)")
        store = cls(pool, cfg); store.security_tables = has_reviews
        store.security_required = has_gates
        store.task_tombstone = bool(has_tombstone)
        return store

    async def create(self, task):
        value = task.model_dump(mode="json")
        material = {k: value[k] for k in ("session_id", "intent", "targets", "constraints", "user_request", "operation")}
        if task.task_kind != "ops": material["task_kind"] = task.task_kind
        from .runtime_profile import context
        runtime = context(self.cfg)
        if runtime is not None: material["runtime_context"] = runtime
        hashed = digest(material)
        value = json.loads(self.cfg.redact(encode(value)))
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(90901)")
            row = await c.fetchrow("SELECT task_id,request_hash FROM ma_tasks WHERE user_id=$1 AND idempotency_key=$2", task.actor.user_id, task.idempotency_key)
            if row:
                if row["request_hash"] != hashed: raise HTTPException(409, "idempotency_conflict")
                return row["task_id"], False
            if await c.fetchval("SELECT EXISTS(SELECT 1 FROM ma_session_guards WHERE session_id=$1)", task.session_id):
                raise HTTPException(409, "session_deleting")
            count = await c.fetchval("SELECT count(*) FROM ma_tasks WHERE user_id=$1 AND status=ANY($2::text[])", task.actor.user_id, list(ACTIVE))
            queue = await c.fetchval("SELECT count(*) FROM ma_tasks WHERE status='queued'")
            if count >= self.cfg.user_active_limit or queue >= self.cfg.queue_capacity:
                raise HTTPException(429, "task_capacity", headers={"Retry-After": "2"})
            await c.execute("INSERT INTO ma_tasks(task_id,trace_id,session_id,user_id,parent_turn_id,idempotency_key,request_hash,envelope,status,deadline) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,'queued',now()+$9*interval '1 second')", task.task_id, task.trace_id, task.session_id, task.actor.user_id, task.parent_turn_id, task.idempotency_key, hashed, encode(value), min(task.constraints.timeout_sec, self.cfg.task_timeout))
            if runtime is not None:
                await c.execute("UPDATE ma_tasks SET runtime_context=$2::jsonb WHERE task_id=$1", task.task_id, encode(runtime))
            await c.execute("INSERT INTO ma_outbox(task_id) VALUES($1)", task.task_id)
            await c.execute("INSERT INTO ma_task_events(task_id,to_status,execution_epoch,reason) VALUES($1,'queued',0,'accepted')", task.task_id)
        return task.task_id, True

    async def get(self, tid, user=None):
        row = await self.pool.fetchrow("SELECT * FROM ma_tasks WHERE task_id=$1 AND ($2::bigint IS NULL OR user_id=$2)", tid, user)
        if not row: raise HTTPException(404, "task_not_found")
        value = dict(row)
        value["envelope"], value["result"] = json.loads(value["envelope"]), json.loads(value["result"])
        return value

    async def report(self, tid, user):
        row = await self.get(tid, user)
        steps = await self.pool.fetch("SELECT * FROM ma_task_steps WHERE task_id=$1 ORDER BY step_no", tid)
        calls = await self.pool.fetch("SELECT * FROM ma_tool_calls WHERE task_id=$1 ORDER BY collected_at,call_id", tid)
        def unpack(record):
            r = dict(record)
            r.pop('adapter_claimed_at',None)
            for k in ("args", "result", "data", "error"):
                if isinstance(r.get(k), str): r[k] = json.loads(r[k])
            return r
        result = row["result"]
        report = {"protocol": "ma/v1", "task_id": str(tid), "trace_id": str(row["trace_id"]), "session_id": str(row["session_id"]),
                "parent_turn_id": row["parent_turn_id"], "intent": display_intent(row["envelope"]["intent"]), "status": row["status"],
                "steps": [unpack(r) for r in steps], "calls": [unpack(r) for r in calls],
                "evidence_refs": ["call:" + str(r["call_id"]) for r in calls],
                "diagnosis": result.get("diagnosis", {}), "warnings": result.get("warnings", []), "error": result.get("error"),
                "tool_calls": result.get("tool_calls", []), "sources": result.get("sources", []),
                "verification_results": result.get("verification_results"), "device": result.get("device"),
                "model_calls": row["model_calls"], "link_pending": row["link_pending"], "evidence": build_evidence(result.get("tool_calls", []), self.cfg),
                "created_at": row["created_at"], "finished_at": row["finished_at"]}
        report['task_kind']=row['envelope'].get('task_kind','ops')
        report['business_quality'] = result.get('business_quality')
        report['inspection_report']=result.get('inspection_report')
        report['infrastructure_evidence']=result.get('infrastructure_evidence',[])
        approvals=await self.pool.fetch('SELECT * FROM ma_approvals WHERE task_id=$1 ORDER BY created_at',tid)
        report['approvals']=[{**dict(a),'plan':json.loads(a['plan'])} for a in approvals]
        attempts=await self.pool.fetch('SELECT * FROM ma_execution_attempts WHERE task_id=$1 ORDER BY started_at',tid)
        report['execution_attempts']=[{**dict(a),'evidence':json.loads(a['evidence']) if a['evidence'] else None} for a in attempts]
        report['operation_evidence']=result.get('operation_evidence',[])
        if self.security_tables:
            from .runtime_reviews import public_reviews
            report.update(await public_reviews(self.pool, tid, user))
        return ResultEnvelope.model_validate(report).model_dump(mode='json', exclude_none=False)

    async def listing(self, user, sid=None, status=None, offset=0, limit=20):
        where = "user_id=$1 AND ($2::uuid IS NULL OR session_id=$2) AND ($3::text IS NULL OR status=$3)"
        if self.task_tombstone: where += " AND deleted_at IS NULL"
        rows = await self.pool.fetch("SELECT task_id,status,session_id,parent_turn_id,created_at,finished_at,envelope->>'intent' AS intent FROM ma_tasks WHERE " + where + " ORDER BY created_at DESC,task_id DESC OFFSET $4 LIMIT $5", user, sid, status, offset, limit)
        return {"list": [{**dict(r),'intent':display_intent(r['intent'])} for r in rows], "offset": offset, "page_size": limit}

    async def claim(self, tid, worker):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(90901)")
            running = await c.fetchval("SELECT count(*) FROM ma_tasks WHERE status=ANY($1::text[])", ["planning", "executing"])
            if running >= self.cfg.global_concurrency: return None
            row = await c.fetchrow("UPDATE ma_tasks SET status='planning',execution_epoch=execution_epoch+1,worker_id=$2,lease_until=now()+$3*interval '1 second',updated_at=now() WHERE task_id=$1 AND status='queued' AND NOT cancel_requested AND deadline>now() RETURNING *", tid, worker, self.cfg.lease_seconds)
            if row:
                await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,'queued','planning',$2,'claimed')", tid, row["execution_epoch"])
                return dict(row)

    async def check(self, tid, epoch):
        row = await self.get(tid)
        if row["execution_epoch"] != epoch or row["status"] not in ("planning", "executing") or row["lease_until"] <= datetime.now(timezone.utc): raise LostLease()
        if row["cancel_requested"]: raise InterruptedError("cancelled")
        if row["deadline"] <= datetime.now(timezone.utc): raise TimeoutError()
        return row

    def _write_or_read_context(self, row, epoch):
        if not row or row['execution_epoch'] != epoch or row['status'] not in ('planning', 'executing') or row['lease_until'] <= datetime.now(timezone.utc):
            raise LostLease()
        if row['cancel_requested']: raise InterruptedError()
        if row['deadline'] <= datetime.now(timezone.utc): raise TimeoutError()

    async def heartbeat(self, tid, epoch):
        result = await self.pool.execute("UPDATE ma_tasks SET lease_until=now()+$3*interval '1 second' WHERE task_id=$1 AND execution_epoch=$2 AND status IN ('planning','executing') AND lease_until>now()", tid, epoch, self.cfg.lease_seconds)
        if result != "UPDATE 1": raise LostLease()

    async def transition(self, tid, epoch, status, result=None, reason=None):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT status,execution_epoch,lease_until FROM ma_tasks WHERE task_id=$1 FOR UPDATE", tid)
            if not row or row["execution_epoch"] != epoch or row["status"] in TERMINAL or (epoch > 0 and row["lease_until"] <= datetime.now(timezone.utc)): raise LostLease()
            await c.execute("UPDATE ma_tasks SET status=$3,result=COALESCE($4::jsonb,result),updated_at=now(),finished_at=CASE WHEN $5 THEN now() ELSE finished_at END WHERE task_id=$1 AND execution_epoch=$2", tid, epoch, status, self.cfg.redact(encode(result)) if result is not None else None, status in TERMINAL)
            await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,$3,$4,$5)", tid, row["status"], status, epoch, reason)
            if status in TERMINAL:
                step_status = status if status in ("cancelled", "timeout") else "failed"
                await c.execute("UPDATE ma_task_steps SET status=$3,finished_at=now() WHERE task_id=$1 AND execution_epoch=$2 AND status='running'", tid, epoch, step_status)
                await c.execute("UPDATE ma_tool_calls SET status=$3,finished_at=now() WHERE task_id=$1 AND execution_epoch=$2 AND status='running'", tid, epoch, step_status)

    async def cancel(self, tid, user):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT status,execution_epoch FROM ma_tasks WHERE task_id=$1 AND user_id=$2 FOR UPDATE", tid, user)
            if not row: raise HTTPException(404, "task_not_found")
            if row["status"] not in TERMINAL:
                await c.execute("UPDATE ma_tasks SET cancel_requested=true WHERE task_id=$1", tid)
                if row["status"] in ("queued","awaiting_approval"):
                    await c.execute("UPDATE ma_approvals SET status='cancelled' WHERE task_id=$1 AND status IN ('pending','approved') AND consumed_at IS NULL",tid)
                    await c.execute("UPDATE ma_task_steps SET status='cancelled',finished_at=now() WHERE task_id=$1 AND status='pending'",tid)
                    await c.execute("UPDATE ma_tasks SET status='cancelled',finished_at=now() WHERE task_id=$1", tid)
                    await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,'cancelled',$3,'user_cancel')", tid,row['status'],row["execution_epoch"])
        return await self.report(tid, user)

    async def soft_delete(self, tid, user):
        # Hide one finished task from the records list. Steps, calls, events, approvals and
        # security reviews are never touched, so the evidence chain stays intact.
        if not self.task_tombstone: raise HTTPException(409, "task_delete_unsupported")
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT status,execution_epoch,deleted_at FROM ma_tasks WHERE task_id=$1 AND user_id=$2 FOR UPDATE", tid, user)
            if not row: raise HTTPException(404, "task_not_found")
            if row["status"] not in TERMINAL: raise HTTPException(409, "task_not_terminal")
            if row["deleted_at"] is not None:
                return {"task_id": str(tid), "deleted": True, "already_deleted": True, "deleted_at": row["deleted_at"]}
            deleted_at = await c.fetchval("UPDATE ma_tasks SET deleted_at=now(),deleted_by=$2 WHERE task_id=$1 RETURNING deleted_at", tid, user)
            await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,$2,$3,'user_deleted')", tid, row["status"], row["execution_epoch"])
        return {"task_id": str(tid), "deleted": True, "already_deleted": False, "deleted_at": deleted_at}

    async def bind(self, tid, user, sid, turn):
        await self.get(tid, user)
        result = await self.pool.execute("UPDATE ma_tasks SET parent_turn_id=$4,link_pending=false WHERE task_id=$1 AND user_id=$2 AND session_id=$3 AND (parent_turn_id IS NULL OR parent_turn_id=$4)", tid, user, sid, turn)
        if result != "UPDATE 1": raise HTTPException(409, "turn_binding_conflict")
        return {"bound": True}

    async def guard(self, sid, user, op, action):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(90901)")
            row = await c.fetchrow("SELECT * FROM ma_session_guards WHERE session_id=$1 FOR UPDATE", sid)
            if row and row["user_id"] != user: raise HTTPException(404, "session_not_found")
            if action == "reserve":
                if row and row["operation_id"] != op: raise HTTPException(409, "session_deleting")
                if await c.fetchval("SELECT EXISTS(SELECT 1 FROM ma_tasks WHERE session_id=$1 AND status=ANY($2::text[]))", sid, list(ACTIVE)): raise HTTPException(409, "session_has_active_tasks")
                await c.execute("INSERT INTO ma_session_guards(session_id,user_id,operation_id,state) VALUES($1,$2,$3,'reserved') ON CONFLICT DO NOTHING", sid, user, op)
            elif not row or row["operation_id"] != op: raise HTTPException(409, "delete_operation_conflict")
            elif action == "release":
                if row["state"] == "deleted": raise HTTPException(409, "session_deleted")
                await c.execute("DELETE FROM ma_session_guards WHERE session_id=$1", sid)
            else: await c.execute("UPDATE ma_session_guards SET state='deleted' WHERE session_id=$1", sid)
        return {"action": action}

    async def recover(self, startup=False, task_id=None):
        async with self.pool.acquire() as c, c.transaction():
            await c.execute("SELECT pg_advisory_xact_lock(90901)")
            rows = await c.fetch("SELECT * FROM ma_tasks WHERE status=ANY($1::text[]) AND ($2 OR deadline<=now() OR lease_until<=now()) AND ($3::uuid IS NULL OR task_id=$3) FOR UPDATE", list(ACTIVE), startup,task_id)
            for r in rows:
                a=await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1',r['task_id'])
                if r['status']=='awaiting_approval':
                    if a and a['status']=='pending' and a['expires_at']>datetime.now(timezone.utc): continue
                    if a: await self._end_approval(c,r,a,'expired','cancelled','approval_expired')
                    continue
                uncertain=await c.fetchval("SELECT EXISTS(SELECT 1 FROM ma_execution_attempts WHERE task_id=$1 AND status='started')",r['task_id'])
                if not uncertain and a and a['status']=='approved' and a['expires_at']<=datetime.now(timezone.utc):
                    await self._end_approval(c,r,a,'expired','cancelled','approval_expired')
                    continue
                expired = r["deadline"] <= datetime.now(timezone.utc)
                status = "failed" if uncertain else "timeout" if expired else "failed"
                code = "execution_uncertain" if uncertain else "timeout" if expired else "auth_context_lost" if r["status"] == "queued" else "worker_interrupted"
                if uncertain: await c.execute("UPDATE ma_execution_attempts SET status='execution_uncertain',finished_at=now() WHERE task_id=$1 AND status='started'",r['task_id'])
                result = json.loads(r["result"]); result["error"] = {"code": code, "message": code, "retryable": False}
                await c.execute("UPDATE ma_tasks SET status=$2,result=$3::jsonb,finished_at=now() WHERE task_id=$1", r["task_id"], status, encode(result))
                await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,$3,$4,$5)", r["task_id"], r["status"], status, r["execution_epoch"], code)
                await c.execute("UPDATE ma_task_steps SET status=$2,finished_at=now() WHERE task_id=$1 AND status='running'", r["task_id"], "timeout" if expired else "failed")
                await c.execute("UPDATE ma_tool_calls SET status=$2,finished_at=now() WHERE task_id=$1 AND status='running'", r["task_id"], "timeout" if expired else "failed")

    async def model_called(self, tid, epoch):
        await self.check(tid, epoch)
        await self.pool.execute("UPDATE ma_tasks SET model_calls=model_calls+1 WHERE task_id=$1 AND execution_epoch=$2", tid, epoch)

    async def begin_call(self, tid, epoch, step, tool, args, trace_id, asset=None):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE", tid)
            if row["execution_epoch"] != epoch or row["status"] != "executing" or row["lease_until"] <= datetime.now(timezone.utc): raise LostLease()
            if row["cancel_requested"]: raise InterruptedError()
            if row["deadline"] <= datetime.now(timezone.utc): raise TimeoutError()
            count = await c.fetchval("SELECT count(*) FROM ma_tool_calls WHERE task_id=$1", tid)
            if count >= self.cfg.max_calls: raise ValueError("tool_budget_exceeded")
            await c.execute("INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,status,execution_epoch) VALUES($1,$2,$3,$4::jsonb,$5,'running',$6) ON CONFLICT(task_id,step_no) DO UPDATE SET status='running'", tid, step, tool, encode(args), digest(args), epoch)
            call_id = uuid4()
            await c.execute("INSERT INTO ma_tool_calls(call_id,task_id,step_no,trace_id,execution_epoch,args_hash,status) VALUES($1,$2,$3,$4,$5,$6,'running')", call_id, tid, step, trace_id, epoch, digest(args))
            if asset:
                await c.execute('UPDATE ma_task_steps SET asset_id=$3,asset_version=$4 WHERE task_id=$1 AND step_no=$2',tid,step,asset['asset_id'],asset['version'])
                await c.execute('UPDATE ma_tool_calls SET asset_id=$2,asset_version=$3 WHERE call_id=$1',call_id,asset['asset_id'],asset['version'])
        return call_id

    async def end_call(self, tid, epoch, step, call_id, data, error, duration, result):
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow("SELECT status,execution_epoch,lease_until FROM ma_tasks WHERE task_id=$1 FOR UPDATE", tid)
            if row["execution_epoch"] != epoch or row["status"] != "executing" or row["lease_until"] <= datetime.now(timezone.utc): raise LostLease()
            status = "failed" if error else "succeeded"
            await c.execute("UPDATE ma_tool_calls SET status=$3,data=$4::jsonb,error=$5::jsonb,duration_ms=$6,finished_at=now() WHERE call_id=$1 AND execution_epoch=$2", call_id, epoch, status, self.cfg.redact(encode(data)), encode(error) if error else None, duration)
            await c.execute("UPDATE ma_task_steps SET status=$4,result=$5::jsonb,error=$6::jsonb,finished_at=now() WHERE task_id=$1 AND step_no=$2 AND execution_epoch=$3", tid, step, epoch, status, self.cfg.redact(encode(data)), encode(error) if error else None)
            await c.execute("UPDATE ma_tasks SET result=$3::jsonb WHERE task_id=$1 AND execution_epoch=$2", tid, epoch, self.cfg.redact(encode(result)))

    async def import_assets(self,assets,registry_hash,user):
        indexed=validate_catalog(assets)
        if digest(assets)!=registry_hash: raise HTTPException(409,'registry_mismatch')
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(90903)')
            existing=await c.fetch('SELECT asset_id,version,registry_hash FROM ma_assets ORDER BY asset_id FOR UPDATE')
            for old in existing:
                new=indexed.get(old['asset_id'])
                # Imports are complete snapshots. Removed assets must first be explicitly disabled.
                if not new: raise HTTPException(409,'asset_missing_from_snapshot')
            for a in sorted(assets,key=lambda a:a['kind']!='host'):
                snapshot=canonical_asset(a)
                old=await c.fetchrow('SELECT version,snapshot FROM ma_asset_versions WHERE asset_id=$1 ORDER BY version DESC LIMIT 1',a['asset_id'])
                if old:
                    if a['version']<old['version'] or (a['version']==old['version'] and digest(json.loads(old['snapshot']))!=digest(snapshot)):
                        raise HTTPException(409,'asset_version_conflict')
                    if a['version']>old['version']+1: raise HTTPException(409,'asset_version_conflict')
                elif a['version']!=1: raise HTTPException(409,'asset_version_conflict')
                await c.execute('''INSERT INTO ma_assets(asset_id,kind,display_name,project_scope,parent_id,enabled,criticality,environment,version,verified_at,connection_ref,identity_hash,capabilities,registry_hash)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb,$14)
                    ON CONFLICT(asset_id) DO UPDATE SET kind=EXCLUDED.kind,display_name=EXCLUDED.display_name,project_scope=EXCLUDED.project_scope,
                    parent_id=EXCLUDED.parent_id,enabled=EXCLUDED.enabled,criticality=EXCLUDED.criticality,environment=EXCLUDED.environment,
                    version=EXCLUDED.version,verified_at=EXCLUDED.verified_at,connection_ref=EXCLUDED.connection_ref,
                    identity_hash=EXCLUDED.identity_hash,capabilities=EXCLUDED.capabilities,registry_hash=EXCLUDED.registry_hash,updated_at=now()''',
                    a['asset_id'],a['kind'],a['display_name'],a['project_scope'],a['parent_id'],a['enabled'],a['criticality'],a['environment'],
                    a['version'],datetime.fromisoformat(a['verified_at'].replace('Z','+00:00')) if a['verified_at'] else None,
                    a['connection_ref'],a['identity_hash'],encode(a['capabilities']),registry_hash)
                await c.execute('INSERT INTO ma_asset_versions(asset_id,version,snapshot,imported_by) VALUES($1,$2,$3::jsonb,$4) ON CONFLICT DO NOTHING',
                    a['asset_id'],a['version'],encode(snapshot),user)
        return {'registry_hash':registry_hash,'asset_count':len(assets)}

    async def list_assets(self,kind=None,parent_id=None,keyword=None):
        rows=await self.pool.fetch('SELECT * FROM ma_assets ORDER BY asset_id')
        entries=[]
        for row in rows:
            a=dict(row);a['capabilities']=json.loads(a['capabilities'])
            if kind and a['kind']!=kind or parent_id and a['parent_id']!=parent_id: continue
            if keyword and keyword.casefold() not in (a['asset_id']+' '+a['display_name']).casefold(): continue
            entries.append(public_asset(a))
        return {'list':entries,'total':len(entries),'complete':True,'slow_queries_available':False}

    async def resolve_asset(self,aid,tool):
        row=await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',aid)
        if not row: raise ValueError('asset_not_found')
        asset=dict(row);asset['capabilities']=json.loads(asset['capabilities'])
        if not asset['enabled'] or not asset['verified_at']: raise ValueError('asset_not_verified')
        kind='container' if tool=='docker.restart_drill_container' else TOOLS[tool][0]
        if asset['kind']!=kind or tool not in asset['capabilities']: raise ValueError('capability_unavailable')
        if asset['parent_id']:
            parent=await self.pool.fetchrow('SELECT enabled,verified_at,registry_hash FROM ma_assets WHERE asset_id=$1',asset['parent_id'])
            if not parent or not parent['enabled'] or not parent['verified_at'] or parent['registry_hash']!=asset['registry_hash']: raise ValueError('asset_not_verified')
        return asset

    async def claim_execution(self,grant):
        from uuid import UUID
        tid=UUID(grant['task_id']);cid=UUID(grant['call_id'])
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            if not row or row['execution_epoch']!=grant['execution_epoch'] or row['status']!='executing' or row['cancel_requested'] or row['lease_until']<=datetime.now(timezone.utc) or row['deadline']<=datetime.now(timezone.utc):
                raise HTTPException(403,'execution_context_lost')
            if row['user_id']!=grant['user_id'] or str(row['trace_id'])!=grant['trace_id'] or abs(row['deadline'].timestamp()-grant['deadline'])>.01: raise HTTPException(403,'context_mismatch')
            call=await c.fetchrow('SELECT c.*,s.tool FROM ma_tool_calls c JOIN ma_task_steps s USING(task_id,step_no) WHERE c.call_id=$1 FOR UPDATE OF c',cid)
            if not call or call['status']!='running' or call['task_id']!=tid: raise HTTPException(403,'execution_context_lost')
            for field in ('execution_epoch','step_no','args_hash','asset_id','asset_version','tool'):
                if call[field]!=grant[field]: raise HTTPException(403,'context_mismatch')
            current=await c.fetchrow('SELECT version,registry_hash,enabled,verified_at FROM ma_assets WHERE asset_id=$1 FOR SHARE',grant['asset_id'])
            if not current or not current['enabled'] or not current['verified_at'] or current['version']!=grant['asset_version'] or current['registry_hash']!=grant['registry_hash']:
                raise HTTPException(403,'asset_version_changed')
            if call['adapter_claimed_at'] is not None: raise HTTPException(409,'dispatch_replayed')
            if self.security_gates and call['tool'] in __import__('ops_agent.runtime_reviews', fromlist=['SENSITIVE']).SENSITIVE:
                await self.security_gates.validate_in_tx(c,tid,call['step_no'],grant['execution_epoch'],'sensitive_read',call['args_hash'])
            await c.execute('UPDATE ma_tool_calls SET adapter_claimed_at=now() WHERE call_id=$1',cid)
        return {'authorized':True}


def canonical_asset(asset):
    # Keep the imported timestamp string byte-for-byte for immutable version comparison.
    return {k:asset[k] for k in __import__('ops_contract').ASSET_FIELDS}
