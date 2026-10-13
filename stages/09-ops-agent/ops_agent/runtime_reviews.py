"""SEC-M2 program gates. M1 synthetic controller remains separately restricted."""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

try:
    import review_contract as contract
except ModuleNotFoundError:
    # Repository-local shared protocol; packaging must include this exact source.
    source = Path(__file__).resolve().parents[2] / '10-security-agent/review_contract.py'
    spec = importlib.util.spec_from_file_location('review_contract', source)
    contract = importlib.util.module_from_spec(spec)
    sys.modules['review_contract'] = contract
    spec.loader.exec_module(contract)

from .reviews import ReviewController, ControlFailure
from ops_contract import digest, validate_args
from ops_write_contract import TOOL, explicit_restart, restart_args, matches

SENSITIVE = frozenset({'linux.get_system_logs', 'docker.get_container_logs',
                       'docker.inspect_container', 'postgresql.get_connection_stats'})
ERRORS = frozenset({'security_denied', 'security_clarification_required', 'security_unavailable',
                    'security_invalid', 'security_review_stale', 'security_projection_blocked'})


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def sensitive_report(report):
    return bool(report.get('infrastructure_evidence') or report.get('operation_evidence') or report.get('approvals'))


def validate_payload(payload):
    if not isinstance(payload,dict) or set(payload)!={'task_status','evidence','actions'}:
        raise ValueError('security_projection_blocked')
    if payload['task_status'] not in ('queued','planning','executing','awaiting_approval','succeeded','failed','cancelled','timeout'):
        raise ValueError('security_projection_blocked')
    if not isinstance(payload['evidence'],list) or not isinstance(payload['actions'],list): raise ValueError('security_projection_blocked')
    for e in payload['evidence']:
        if not isinstance(e,dict) or set(e)-{'evidence_ref','tool','asset_id','asset_version','status','complete','truncated','collected_at','sampled_at','state','assets'}:
            raise ValueError('security_projection_blocked')
        if not isinstance(e.get('state',{}),dict) or set(e.get('state',{}))-{'running','health','state','image_digest'}:
            raise ValueError('security_projection_blocked')
        for a in e.get('assets',[]):
            if not isinstance(a,dict) or set(a)-{'asset_id','kind','parent_id','version','criticality','enabled'}:
                raise ValueError('security_projection_blocked')
    for a in payload['actions']:
        if not isinstance(a,dict) or set(a)!={'status'} or a['status'] not in ('started','succeeded','failed','execution_uncertain'):
            raise ValueError('security_projection_blocked')
    contract.safe_text(contract.canonical(payload).decode())
    return payload


def licensed_summary(report):
    """Only fixed scalar state/aliases/hashes; no user request, model prose or raw tool body."""
    rows = []
    for e in report.get('infrastructure_evidence', []):
        value={k: e[k] for k in ('evidence_ref', 'tool', 'asset_id', 'asset_version', 'status',
                                      'complete', 'truncated', 'collected_at', 'sampled_at') if k in e}
        calls=[c for c in report.get('tool_calls',[]) if c.get('call_id')==e.get('call_id')]
        if calls:
            data=calls[-1].get('result',{})
            value['state']={k:data[k] for k in ('running','health','state','image_digest') if k in data}
            if e.get('tool')=='assets.list':
                if any(a.get('environment') not in ('acceptance','fixture') for a in data.get('list',[])):
                    raise ValueError('security_projection_blocked')
                value['assets']=[{k:a[k] for k in ('asset_id','kind','parent_id','version','criticality','enabled') if k in a} for a in data.get('list',[])]
        rows.append(value)
    return validate_payload({'task_status': report['status'], 'evidence': rows,
            'actions': [{'status': a['status']} for a in report.get('execution_attempts', [])]})


async def public_reviews(pool, tid, user):
    rows = await pool.fetch('SELECT * FROM ma_security_reviews WHERE task_id=$1 AND user_id=$2 ORDER BY created_at,review_id', tid, user)
    reviews = []
    for r in rows:
        decision = unpack(r['decision']) or {}
        reviews.append({'review_id': str(r['review_id']), 'step_no': r['step_no'], 'kind': r['kind'],
            'state': r['state'], 'decision': decision.get('decision'), 'reason': decision.get('reason'),
            'rule_hits': decision.get('rule_hits', []), 'evidence_refs': decision.get('evidence_refs', []),
            'execution_epoch': r['execution_epoch'], 'created_at': r['created_at'], 'finished_at': r['finished_at'],
            'expires_at': r['expires_at'], 'error_code': r['error_code'],
            'policy_version': unpack(r['request'])['policy_version'], 'duration_ms': r['duration_ms']})
    events = await pool.fetch('SELECT e.event_seq,e.review_id,e.state,e.reason,e.created_at FROM ma_security_review_events e JOIN ma_security_reviews r USING(review_id) WHERE r.task_id=$1 AND r.user_id=$2 ORDER BY e.event_seq', tid, user)
    return {'security_reviews': reviews, 'security_events': [dict(e) for e in events]}


class RuntimeController(ReviewController):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.prepared={}

    async def reserve(self,request):
        raw=request.model_dump(mode='json');request_hash=digest(raw)
        context=self.prepared.get(request.review_id)
        async with self.pool.acquire() as c,c.transaction():
            await c.execute('SELECT pg_advisory_xact_lock(100101)')
            existing=await c.fetchrow('SELECT request_hash,user_id FROM ma_security_reviews WHERE review_id=$1',request.review_id)
            if existing:
                if existing['user_id']!=request.actor.user_id:raise ControlFailure('review_not_found')
                if existing['request_hash']!=request_hash:raise ControlFailure('review_idempotency_conflict')
                return False
            if context is None or request.deadline<=datetime.now(timezone.utc):raise ControlFailure('security_review_stale')
            await c.execute('INSERT INTO ma_security_gate_contexts(review_id,task_id,step_no,execution_epoch,user_id,role,kind,material,material_hash) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9)',request.review_id,request.task_id,request.step_no,request.execution_epoch,request.actor.user_id,request.actor.role,request.kind,json.dumps(context['material']),context['material_hash'])
            await c.execute("INSERT INTO ma_security_reviews(review_id,task_id,step_no,trace_id,user_id,execution_epoch,kind,request_hash,binding_hash,request,state,model) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9::jsonb,'pending',$10)",request.review_id,request.task_id,request.step_no,request.trace_id,request.actor.user_id,request.execution_epoch,request.kind,request_hash,json.dumps(raw),self.model)
            await c.execute("INSERT INTO ma_security_review_events(review_id,state,reason) VALUES($1,'pending','review_started')",request.review_id)
        return True

    async def check_database(self):
        if not self.database_checked:
            if await self.pool.fetchval('SELECT current_database()') not in ('sec_m2_acceptance_test', 'sec_m2_regression_test'):
                raise ControlFailure('isolated_sec_m2_database_required')
            self.database_checked = True

    async def build(self, task_id, step_no, user_id, role, review_id, kind):
        await self.check_database()
        task = await self.pool.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 AND user_id=$2', task_id, user_id)
        context = await self.pool.fetchrow('SELECT * FROM ma_security_gate_contexts WHERE review_id=$1 AND task_id=$2 AND user_id=$3', review_id, task_id, user_id)
        if context is None:context=self.prepared.get(review_id)
        if not task or not context: raise ControlFailure('review_not_found')
        if context['role'] != role or context['kind'] != kind or context['step_no'] != step_no:
            raise ControlFailure('security_invalid')
        if context['execution_epoch'] != task['execution_epoch']:
            raise ControlFailure('security_review_stale')
        if kind not in ('data_egress', 'post_execution') and (task['status'] not in ('planning', 'executing') or task['cancel_requested']):
            raise ControlFailure('security_review_stale')
        step = await self.pool.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 AND step_no=$2', task_id, step_no)
        if not step or step['execution_epoch'] != task['execution_epoch']: raise ControlFailure('security_review_stale')
        args = unpack(step['args']); material = unpack(context['material'])
        if digest(material) != context['material_hash'] or digest(args) != step['args_hash'] or material['args_hash'] != step['args_hash']:
            raise ControlFailure('security_invalid')
        for binding in material.get('registry_bindings',[]):
            a=await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',binding['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version']!=binding['version'] or a['identity_hash']!=binding['identity_hash']:
                raise ControlFailure('security_review_stale')
        from .executor import MANIFEST
        from .write import MANIFEST as WRITE_MANIFEST
        entry = WRITE_MANIFEST if step['tool'] == TOOL else MANIFEST.get(step['tool'])
        if not entry or step['version'] != entry['version'] or step['risk_class'] != entry['risk_class']:
            raise ControlFailure('security_invalid')
        if step['tool'] == TOOL: restart_args(args)
        elif step['tool'] != 'assets.list': validate_args(step['tool'], args)
        asset = None
        if step['asset_id']:
            a = await self.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1', step['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version'] != step['asset_version']:
                raise ControlFailure('security_review_stale')
            if a['environment'] not in ('fixture', 'acceptance'): raise ControlFailure('security_projection_blocked')
            if a['identity_hash'] != material['asset_identity_hash']: raise ControlFailure('security_review_stale')
            if args.get('asset_id') != a['asset_id']: raise ControlFailure('security_invalid')
            asset = {k: a[k] for k in ('asset_id', 'version', 'identity_hash', 'project_scope', 'criticality')}
            asset['verified'] = True
        if step['tool'] == TOOL and (role != 'admin' or not asset or asset['criticality'] != 'drill'):
            raise ControlFailure('security_invalid')
        request = contract.ReviewRequest(protocol='ma-review/v1', review_id=review_id, task_id=task_id,
            trace_id=task['trace_id'], step_no=step_no, execution_epoch=task['execution_epoch'], kind=kind,
            actor={'user_id': user_id, 'role': role}, tool={'name': step['tool'], 'version': step['version'],
                'risk_class': step['risk_class'], 'args_hash': step['args_hash'], 'plan_hash': material['plan_hash']},
            asset=asset, policy_version=contract.RUNTIME_POLICY, scope_version=contract.RUNTIME_SCOPE,
            normalized_intent=material['intent'], evidence=material['evidence'], egress=material.get('egress'),
            deadline=material['deadline'])
        contract.projection(request, (self.service_token, self.request_key, self.result_key))
        return request


class SecurityGates:
    def __init__(self, cfg, store, client):
        self.cfg, self.store = cfg, store
        self.controller = RuntimeController(store.pool, client, cfg.security_service_token,
            cfg.security_request_key, cfg.security_result_key, url=cfg.security_url)

    async def prepare_step(self, row, epoch, step, name, args, asset):
        async with self.store.pool.acquire() as c, c.transaction():
            current = await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE', row['task_id'])
            self.store._write_or_read_context(current, epoch)
            old = await c.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 AND step_no=$2 FOR UPDATE', row['task_id'], step)
            if old and (old['tool'] != name or old['args_hash'] != digest(args)):
                raise ValueError('security_invalid')
            if old:
                await c.execute('UPDATE ma_task_steps SET execution_epoch=$3 WHERE task_id=$1 AND step_no=$2', row['task_id'], step, epoch)
            else:
                await c.execute("INSERT INTO ma_task_steps(task_id,step_no,tool,args,args_hash,risk_class,status,execution_epoch,asset_id,asset_version) VALUES($1,$2,$3,$4::jsonb,$5,$6,'pending',$7,$8,$9)", row['task_id'], step, name, json.dumps(args), digest(args), 'R2' if name == TOOL else 'R0', epoch, asset['asset_id'] if asset else None, asset['version'] if asset else None)

    async def require(self, executor, row, epoch, step, name, args, jwt, result, kind, plan=None, payload=None, receiver='operation', facts=None, history=False):
        """No network request until identity, scope, safe material and durable audit pass."""
        actor = await executor.identity(jwt)
        if actor['user_id'] != row['user_id'] or actor['role'] not in ('admin', 'operator'):
            raise ValueError('security_invalid')
        current = await self.store.get(row['task_id'], row['user_id'])
        if current['execution_epoch'] != epoch: raise ValueError('security_review_stale')
        if not history: await self.store.check(row['task_id'], epoch)
        asset = await self.store.resolve_asset(args['asset_id'], name) if args.get('asset_id') else None
        if asset and asset['environment'] not in ('fixture', 'acceptance'):
            raise ValueError('security_projection_blocked')
        if name == TOOL and actor['role'] != 'admin': raise ValueError('security_invalid')
        if not history: await self.prepare_step(row, epoch, step, name, args, asset)
        env = current['envelope']
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=30) if history else current['deadline']
        values = {'identity_verified': True, 'ownership_verified': True, 'tool_registered': True,
            'args_validated': True, 'audit_ready': True, 'target_matches': True,
            'evidence_sufficient': True, 'asset_verified': bool(asset), 'explicit_execution': explicit_restart(env['user_request']),
            'plan_bypasses_approval': False, 'approval_status': 'pending', **(facts or {})}
        if name != TOOL:
            for key in ('explicit_execution','plan_bypasses_approval','approval_status'): values.pop(key,None)
        if asset is None: values.pop('asset_verified',None)
        # Facts only come from this program's call sites and are strictly scalar/allowlisted.
        # Short request-local references remain bound by the signed complete request.
        evidence = [{'ref': 'P' + str(step),
            'source': 'program', 'collected_at': now.isoformat(), 'complete': True, 'filtered': True,
            'truncated': False, 'facts': [{'key': k, 'value': v} for k, v in values.items()]}]
        material = {'args_hash': digest(args), 'asset_identity_hash': asset['identity_hash'] if asset else None,
            'plan_hash': digest(plan or {'tool': name, 'version': '1.0', 'args': args, 'asset': asset['identity_hash'] if asset else None}),
            'intent': {'sensitive_read': '审查本项目登记本机演练目标的固定安全字段读取',
                       'write_plan': '审查本机隔离无状态演练容器重启提案，随后人工审批',
                       'write_preflight': '审查已人工批准的本机演练容器保存计划',
                       'post_execution': '复核本机演练已保存动作事实，禁止自动补写',
                       'data_egress': '审查许可内本机演练状态摘要发往现有网关'}[kind],
            'evidence': evidence, 'deadline': deadline.isoformat()}
        if kind == 'data_egress':
            validate_payload(payload)
            bindings=[]
            for e in payload['evidence']:
                for a in e.get('assets',[]):
                    current_asset=await self.store.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',a['asset_id'])
                    if not current_asset or not current_asset['enabled'] or not current_asset['verified_at'] or current_asset['version']!=a['version'] or current_asset['environment'] not in ('fixture','acceptance'):
                        raise ValueError('security_projection_blocked')
                    bindings.append({k:current_asset[k] for k in ('asset_id','version','identity_hash')})
            material['registry_bindings']=bindings
            contract.safe_text(contract.canonical(payload).decode(), (jwt, *self.cfg_secrets()))
            if len(contract.canonical(payload)) > 32768: raise ValueError('security_projection_blocked')
            values['egress_permitted'] = True
            values['registry_verified'] = True
            values['payload_bound'] = True
            values['material_hash'] = digest(payload)
            evidence[0]['facts'] = [{'key': k, 'value': v} for k, v in values.items()]
            material['egress'] = {'destination': 'existing-gateway', 'scope_version': contract.RUNTIME_SCOPE,
                'receiver': receiver, 'model': 'deepseek-v4.1-flash' if receiver == 'supervisor' else 'deepseek-v4-flash-0731',
                'fields': ['normalized_intent', 'tool', 'asset', 'evidence', 'policy_version'], 'payload_hash': digest(payload)}
        rid = uuid4()
        from .write import audit
        audit(executor, {'event': 'security_gate_ready', 'task_id': str(row['task_id']), 'step_no': step,
                         'kind': kind, 'material_hash': digest(material)})
        self.controller.prepared[rid]={'role':actor['role'],'kind':kind,'step_no':step,'execution_epoch':epoch,'material':material,'material_hash':digest(material)}
        try:
            record = await self.controller.submit(row['task_id'], step, row['user_id'], actor['role'], rid, kind)
            request = await self.controller.build(row['task_id'], step, row['user_id'], actor['role'], rid, kind)
            if not await self.controller.valid(rid, row['user_id'], request):
                raise ValueError('security_invalid' if record['state'] == 'invalid' else 'security_unavailable')
        except ControlFailure as exc:
            raise ValueError(exc.code if exc.code in ERRORS else 'security_invalid') from None
        finally:
            self.controller.prepared.pop(rid,None)
        if record['decision']['decision'] != 'allow':
            result.setdefault('diagnosis', {})['clarification'] = record['decision']['reason']
            raise ValueError('security_denied' if record['decision']['decision'] == 'deny' else 'security_clarification_required')
        # Reauthenticate after the await; a decision is never a substitute for live identity.
        latest = await executor.identity(jwt)
        if latest['user_id'] != actor['user_id'] or latest['role'] != actor['role']:
            raise ValueError('security_review_stale')
        if not history: await self.store.check(row['task_id'], epoch)
        return rid

    async def check_egress(self,tid,rid,user,payload_hash,receiver):
        async with self.store.pool.acquire() as c,c.transaction():
            task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 AND user_id=$2 FOR SHARE',tid,user)
            if not task:raise ValueError('security_review_stale')
            review=await c.fetchrow('SELECT r.*,g.material FROM ma_security_reviews r JOIN ma_security_gate_contexts g USING(review_id) WHERE review_id=$1 AND r.task_id=$2 AND r.user_id=$3',rid,tid,user)
            if not review:raise ValueError('security_review_stale')
            material=unpack(review['material']);egress=material.get('egress',{})
            if egress.get('payload_hash')!=payload_hash or egress.get('receiver')!=receiver:raise ValueError('security_review_stale')
            if task['execution_epoch']!=review['execution_epoch']:raise ValueError('security_review_stale')
            if receiver=='operation':self.store._write_or_read_context(task,review['execution_epoch'])
            current=await self.validate_in_tx(c,tid,review['step_no'],review['execution_epoch'],'data_egress',material['args_hash'])
            if current!=rid:raise ValueError('security_review_stale')
        return True

    def cfg_secrets(self):
        return (self.cfg.security_service_token, self.cfg.security_request_key, self.cfg.security_result_key,
                self.cfg.llm_key, self.cfg.service_token, self.cfg.actor_key, self.cfg.envelope_key)

    async def validate_in_tx(self, c, tid, step, epoch, kind, args_hash, plan=None):
        """Final gate checked under the caller's task/asset locks, before consuming any grant."""
        row = await c.fetchrow("SELECT r.*,g.material,g.material_hash FROM ma_security_reviews r JOIN ma_security_gate_contexts g USING(review_id) WHERE r.task_id=$1 AND r.step_no=$2 AND r.execution_epoch=$3 AND r.kind=$4 ORDER BY r.created_at DESC LIMIT 1 FOR SHARE OF r,g", tid, step, epoch, kind)
        if not row: raise ValueError('security_review_stale')
        material = unpack(row['material']); request = unpack(row['request']); decision = unpack(row['decision'])
        if row['state'] != 'completed' or not row['signature_validated'] or not decision or decision['decision'] != 'allow':
            raise ValueError('security_denied')
        if not row['expires_at'] or row['expires_at'] <= datetime.now(timezone.utc): raise ValueError('security_review_stale')
        if row['binding_hash'] != digest(request) or row['decision_hash'] != digest(decision) or digest(material) != row['material_hash']:
            raise ValueError('security_invalid')
        if request['policy_version'] != contract.RUNTIME_POLICY or request['scope_version'] != contract.RUNTIME_SCOPE or material['args_hash'] != args_hash:
            raise ValueError('security_review_stale')
        if plan is not None and material['plan_hash'] != digest(plan): raise ValueError('security_review_stale')
        if plan is not None and plan.get('security_policy')!=contract.RUNTIME_POLICY:raise ValueError('security_review_stale')
        if request.get('asset'):
            a = await c.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1 FOR SHARE', request['asset']['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version'] != request['asset']['version'] or a['identity_hash'] != request['asset']['identity_hash']:
                raise ValueError('security_review_stale')
        for binding in material.get('registry_bindings',[]):
            a=await c.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1 FOR SHARE',binding['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version']!=binding['version'] or a['identity_hash']!=binding['identity_hash']:
                raise ValueError('security_review_stale')
        return row['review_id']
