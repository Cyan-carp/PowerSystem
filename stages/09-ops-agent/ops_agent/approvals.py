"""Database-owned approval and attempt lifecycle; JWTs never persist."""
import json
from datetime import datetime, timezone
from uuid import UUID, uuid4
from fastapi import HTTPException
from ops_contract import digest
from ops_write_contract import TOOL, matches, proof, explicit_restart


def encode(v): return json.dumps(v,ensure_ascii=False,default=str)


def saved_plan(args, snapshot, security_policy=None):
    return {**({'security_policy':security_policy} if security_policy else {}), 'tool': TOOL, 'tool_version': '1.0', 'args': args, 'args_hash': digest(args), 'snapshot': snapshot,
            'risk_class': 'R2', 'target': snapshot['asset_id'],
            'impact': '仅重启本轮隔离演练容器，期间健康服务暂时不可用；10秒宽限后可能强制终止进程。',
            'rollback_limit': '无持久卷与业务数据；重启没有自动回滚，恢复镜像或配置不等于回滚数据。'}


class ApprovalStore:
    async def prepare_approval(self,tid,epoch,step,args,snapshot,policy,result):
        proof(snapshot)
        if digest(policy)!=snapshot['policy_version']: raise ValueError('invalid_policy')
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(90903)')
            row=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            self._write_context(row,epoch)
            envelope=json.loads(row['envelope'])
            if envelope.get('operation')!='restart_drill_container' or envelope['constraints']['max_risk_class']!='R2' or envelope['actor']['role']!='admin' or not explicit_restart(envelope['user_request']):
                raise ValueError('guard_blocked')
            await self._check_snapshot(c,snapshot)
            old=await c.fetchval('SELECT snapshot FROM ma_policy_versions WHERE version=$1',snapshot['policy_version'])
            if old is not None and digest(json.loads(old))!=digest(policy): raise ValueError('policy_version_changed')
            await c.execute('INSERT INTO ma_policy_versions(version,snapshot) VALUES($1,$2::jsonb) ON CONFLICT DO NOTHING',snapshot['policy_version'],encode(policy))
            aid=uuid4()
            security_policy=None
            if self.security_gates:
                from .runtime_reviews import contract
                security_policy=contract.RUNTIME_POLICY
            plan=saved_plan(args,snapshot,security_policy)
            if self.security_gates:
                await self.security_gates.validate_in_tx(c,tid,step,epoch,'write_plan',digest(args),plan)
            await c.execute("INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,risk_class,status,execution_epoch,asset_id,asset_version) VALUES($1,$2,$3,$4::jsonb,$5,'R2','pending',$6,$7,$8) ON CONFLICT(task_id,step_no) DO NOTHING",tid,step,TOOL,encode(args),digest(args),epoch,snapshot['asset_id'],snapshot['asset_version'])
            await c.execute("INSERT INTO ma_approvals(approval_id,task_id,step_no,plan,args_hash,policy_version,status,expires_at) VALUES($1,$2,$3,$4::jsonb,$5,$6,'pending',now()+$7*interval '1 second')",aid,tid,step,encode(plan),digest(args),snapshot['policy_version'],self.cfg.approval_timeout_seconds)
            await c.execute("UPDATE ma_tasks SET status='awaiting_approval',lease_until=NULL,worker_id=NULL,deadline=now()+$3*interval '1 second',result=$4::jsonb WHERE task_id=$1 AND execution_epoch=$2",tid,epoch,self.cfg.approval_timeout_seconds,encode(result))
            await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,'awaiting_approval',$3,'approval_created')",tid,row['status'],epoch)
        return aid

    def _write_context(self,row,epoch):
        if self.security_required and not self.security_gates: raise HTTPException(503,'security_unavailable')
        if not self.cfg.enabled or not self.cfg.write_enabled: raise HTTPException(403,'write_disabled')
        if not row or row['execution_epoch']!=epoch or row['status'] not in ('planning','executing') or row['lease_until']<=datetime.now(timezone.utc) or row['deadline']<=datetime.now(timezone.utc) or row['cancel_requested']:
            raise HTTPException(409,'execution_context_lost')

    async def _check_snapshot(self,c,snapshot):
        current=await c.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1 FOR SHARE',snapshot['asset_id'])
        if not current or not current['enabled'] or not current['verified_at'] or current['kind']!='container' or current['criticality']!='drill' or current['environment'] not in ('acceptance','fixture') or current['version']!=snapshot['asset_version'] or current['registry_hash']!=snapshot['registry_hash'] or TOOL not in json.loads(current['capabilities']):
            raise ValueError('asset_version_changed')
        parent=await c.fetchrow('SELECT enabled,verified_at,registry_hash FROM ma_assets WHERE asset_id=$1 FOR SHARE',current['parent_id'])
        if not parent or not parent['enabled'] or not parent['verified_at'] or parent['registry_hash']!=current['registry_hash']: raise ValueError('asset_version_changed')

    async def approval(self,tid,aid,user):
        await self.get(tid,user)
        row=await self.pool.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 AND approval_id=$2',tid,aid)
        if not row: raise HTTPException(404,'approval_not_found')
        return {**dict(row),'plan':json.loads(row['plan'])}

    async def decide(self,tid,aid,user,decision,current=None):
        invalid=False
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(90901)')
            await c.execute('SELECT pg_advisory_xact_lock(90903)')
            task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 AND user_id=$2 FOR UPDATE',tid,user)
            if not task: raise HTTPException(404,'task_not_found')
            a=await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 AND approval_id=$2 FOR UPDATE',tid,aid)
            if not a: raise HTTPException(404,'approval_not_found')
            expected='approved' if decision=='approve' else 'rejected'
            if a['status']==expected: return False
            if a['status']!='pending': raise HTTPException(409,'approval_decision_conflict')
            if task['status']!='awaiting_approval' or task['cancel_requested']: raise HTTPException(409,'approval_state_conflict')
            if a['expires_at']<=datetime.now(timezone.utc):
                await self._end_approval(c,task,a,'expired','cancelled','approval_expired')
                invalid=True
            elif decision=='reject':
                await c.execute("UPDATE ma_approvals SET decided_at=now() WHERE approval_id=$1",aid)
                await self._end_approval(c,task,a,'rejected','cancelled','approval_rejected')
            else:
                if not self.cfg.enabled or not self.cfg.write_enabled: raise HTTPException(403,'write_disabled')
                plan=json.loads(a['plan'])
                try:
                    if self.security_required:
                        from .runtime_reviews import contract
                        if plan.get('security_policy')!=contract.RUNTIME_POLICY:raise ValueError('policy_version_changed')
                    proof(current)
                    if not matches(plan['snapshot'],current) or digest(plan['args'])!=a['args_hash']: raise ValueError('approval_invalidated')
                    await self._check_snapshot(c,current)
                    policy=await c.fetchval('SELECT version FROM ma_policy_versions ORDER BY activated_at DESC,version DESC LIMIT 1')
                    if policy!=a['policy_version']: raise ValueError('policy_version_changed')
                except (ValueError,TypeError,KeyError):
                    await self._end_approval(c,task,a,'invalidated','failed','approval_invalidated')
                    invalid=True
                if not invalid:
                    if await c.fetchval("SELECT count(*) FROM ma_tasks WHERE status='queued'")>=self.cfg.queue_capacity:
                        raise HTTPException(429,'task_capacity',headers={'Retry-After':'2'})
                    await c.execute("UPDATE ma_approvals SET status='approved',decided_at=now() WHERE approval_id=$1",aid)
                    await c.execute("UPDATE ma_tasks SET status='queued',deadline=LEAST($2,now()+$3*interval '1 second'),updated_at=now() WHERE task_id=$1",tid,a['expires_at'],self.cfg.task_timeout)
                    await c.execute('INSERT INTO ma_outbox(task_id) VALUES($1)',tid)
                    await c.execute("INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,'awaiting_approval','queued',$2,'approval_approved')",tid,task['execution_epoch'])
        if invalid: raise HTTPException(409,'approval_expired_or_invalidated')
        return True

    async def _end_approval(self,c,task,a,approval_status,status,reason):
        result=json.loads(task['result']);result['error']={'code':reason,'message':reason,'retryable':False}
        await c.execute('UPDATE ma_approvals SET status=$2 WHERE approval_id=$1',a['approval_id'],approval_status)
        await c.execute("UPDATE ma_task_steps SET status=$3,finished_at=now() WHERE task_id=$1 AND step_no=$2 AND status='pending'",task['task_id'],a['step_no'],'cancelled' if status=='cancelled' else 'failed')
        await c.execute('UPDATE ma_tasks SET status=$2,result=$3::jsonb,finished_at=now(),lease_until=NULL WHERE task_id=$1',task['task_id'],status,encode(result))
        await c.execute('INSERT INTO ma_task_events(task_id,from_status,to_status,execution_epoch,reason) VALUES($1,$2,$3,$4,$5)',task['task_id'],task['status'],status,task['execution_epoch'],reason)

    async def approved_plan(self,tid):
        row=await self.pool.fetchrow("SELECT * FROM ma_approvals WHERE task_id=$1 AND status='approved'",tid)
        return {**dict(row),'plan':json.loads(row['plan'])} if row else None

    async def begin_write(self,tid,epoch,a):
        async with self.pool.acquire() as c,c.transaction():
            row=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            self._write_context(row,epoch)
            if a['expires_at']<=datetime.now(timezone.utc): raise ValueError('approval_expired')
            if await c.fetchval('SELECT EXISTS(SELECT 1 FROM ma_execution_attempts WHERE task_id=$1)',tid): raise ValueError('dispatch_replayed')
            if self.security_gates:
                await self.security_gates.validate_in_tx(c,tid,a['step_no'],epoch,'write_preflight',a['args_hash'],a['plan'])
            if await c.fetchval('SELECT count(*) FROM ma_tool_calls WHERE task_id=$1',tid)>=self.cfg.max_calls: raise ValueError('tool_budget_exceeded')
            cid=uuid4()
            await c.execute("UPDATE ma_task_steps SET status='running',execution_epoch=$3,started_at=now() WHERE task_id=$1 AND step_no=$2",tid,a['step_no'],epoch)
            await c.execute("INSERT INTO ma_tool_calls(call_id,task_id,step_no,trace_id,execution_epoch,args_hash,status,asset_id,asset_version) VALUES($1,$2,$3,$4,$5,$6,'running',$7,$8)",cid,tid,a['step_no'],row['trace_id'],epoch,a['args_hash'],a['plan']['snapshot']['asset_id'],a['plan']['snapshot']['asset_version'])
            return cid

    async def consume_write(self,g):
        tid=UUID(g['task_id']);aid=UUID(g['approval_id']);cid=UUID(g['call_id'])
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(90903)')
            task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            self._write_context(task,g['execution_epoch'])
            if task['status']!='executing': raise HTTPException(403,'execution_context_lost')
            if task['user_id']!=g['user_id'] or str(task['trace_id'])!=g['trace_id'] or abs(task['deadline'].timestamp()-g['deadline'])>.01: raise HTTPException(403,'context_mismatch')
            a=await c.fetchrow('SELECT * FROM ma_approvals WHERE task_id=$1 AND approval_id=$2 FOR UPDATE',tid,aid)
            if not a or a['status']!='approved' or a['expires_at']<=datetime.now(timezone.utc): raise HTTPException(403,'approval_not_valid')
            if a['consumed_at'] is not None: raise HTTPException(409,'dispatch_replayed')
            snapshot=json.loads(a['plan'])['snapshot']
            for field in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version'):
                if g[field]!=snapshot[field]: raise HTTPException(403,'context_mismatch')
            await self._check_snapshot(c,snapshot)
            if await c.fetchval('SELECT version FROM ma_policy_versions ORDER BY activated_at DESC,version DESC LIMIT 1')!=a['policy_version']: raise HTTPException(403,'policy_version_changed')
            call=await c.fetchrow('SELECT c.*,s.args,s.tool FROM ma_tool_calls c JOIN ma_task_steps s USING(task_id,step_no) WHERE call_id=$1 FOR UPDATE OF c',cid)
            if not call or call['task_id']!=tid or call['step_no']!=a['step_no'] or g['step_no']!=a['step_no'] or a['args_hash']!=g['args_hash'] or call['tool']!=TOOL or call['status']!='running' or call['execution_epoch']!=g['execution_epoch'] or call['args_hash']!=g['args_hash'] or digest(json.loads(call['args']))!=g['args_hash']: raise HTTPException(403,'context_mismatch')
            if self.security_gates:
                await self.security_gates.validate_in_tx(c,tid,a['step_no'],g['execution_epoch'],'write_preflight',a['args_hash'],json.loads(a['plan']))
            await c.execute("INSERT INTO ma_execution_attempts(attempt_id,task_id,step_no,approval_id,call_id,execution_epoch,status) VALUES($1,$2,$3,$4,$5,$6,'started')",uuid4(),tid,a['step_no'],aid,cid,g['execution_epoch'])
            await c.execute('UPDATE ma_approvals SET consumed_at=now() WHERE approval_id=$1',aid)
            await c.execute('UPDATE ma_tool_calls SET adapter_claimed_at=now() WHERE call_id=$1',cid)
        return {'authorized':True}

    async def finish_attempt(self,cid,status,evidence):
        await self.pool.execute("UPDATE ma_execution_attempts SET status=$2,evidence=$3::jsonb,finished_at=now() WHERE call_id=$1 AND status='started'",cid,status,encode(evidence))
