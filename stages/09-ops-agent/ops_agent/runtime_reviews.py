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
from .runtime_profile import environments
from ops_contract import digest, validate_args
from ops_write_contract import TOOL, explicit_restart, restart_args, matches

SENSITIVE = frozenset({'linux.get_system_logs', 'docker.get_container_logs',
                       'docker.inspect_container', 'postgresql.get_connection_stats'})
METRIC_FIELDS={
    'linux.get_cpu_usage':{'cpu_percent','load_1m','logical_cpus','sample_seconds'},
    'linux.get_memory_usage':{'total_bytes','available_bytes','used_bytes','swap_total_bytes','swap_free_bytes'},
    'linux.get_disk_status':{'filesystem_count','filesystems_total_bytes','filesystems_available_bytes'},
    'linux.list_services':{'registered_service_count','active_service_count','failed_service_count'},
    'docker.list_containers':{'registered_container_count','running_container_count'},
    'docker.get_container_stats':{'cpu_percent','memory_usage_bytes','memory_limit_bytes','memory_working_set_bytes','sample_seconds'},
    'postgresql.get_db_status':{'database_size_bytes','connections','commits','rollbacks','deadlocks'},
    'postgresql.get_connection_stats':{'connections','active','idle','idle_in_transaction','waiting','max_connections'},
}


ERRORS = frozenset({'security_denied', 'security_clarification_required', 'security_unavailable',
                    'security_invalid', 'security_review_stale', 'security_projection_blocked'})


def unpack(value):
    return json.loads(value) if isinstance(value, str) else value


def task_policy(task, cfg=None):
    from .runtime_profile import check_context
    if cfg and cfg.runtime_profile != "legacy":
        saved = check_context(task, cfg)
        return saved["policy_version"], saved["scope_version"]
    env=unpack(task['envelope'])
    return (contract.INSPECTION_POLICY,contract.INSPECTION_SCOPE) if env.get('task_kind')=='security_inspection' else (contract.RUNTIME_POLICY,contract.RUNTIME_SCOPE)


def sensitive_report(report):
    return bool(report.get('inspection_report') or report.get('task_kind')=='security_inspection' or report.get('infrastructure_evidence') or report.get('operation_evidence') or report.get('approvals'))


def validate_payload(payload, cfg=None):
    if not isinstance(payload,dict) or not {'task_status','evidence','actions'} <= set(payload) or set(payload)-{'task_status','evidence','actions','inspection','business_quality'}:
        raise ValueError('security_projection_blocked')
    if payload['task_status'] not in ('queued','planning','executing','awaiting_approval','succeeded','failed','cancelled','timeout'):
        raise ValueError('security_projection_blocked')
    if not isinstance(payload['evidence'],list) or not isinstance(payload['actions'],list): raise ValueError('security_projection_blocked')
    if 'business_quality' in payload:
        q = payload['business_quality']
        if not isinstance(q,dict) or set(q)!={'version','digest','state','fact_refs'} or q['version']!='bnd-business-v1' or not __import__('re').fullmatch('[a-f0-9]{64}', q.get('digest','')) or q['state'] not in ('not_queried','failed','partial_failed','queried'):
            raise ValueError('security_projection_blocked')
        if not isinstance(q['fact_refs'],list) or len(q['fact_refs'])>160 or any(not isinstance(r,str) or not __import__('re').fullmatch('F[a-f0-9]{20}',r) for r in q['fact_refs']):
            raise ValueError('security_projection_blocked')
    for e in payload['evidence']:
        if not isinstance(e,dict) or set(e)-{'evidence_ref','tool','asset_id','asset_version','status','complete','truncated','collected_at','sampled_at','state','assets','metrics'}:
            raise ValueError('security_projection_blocked')
        if not isinstance(e.get('state',{}),dict) or set(e.get('state',{}))-{'running','health','state','image_digest'}:
            raise ValueError('security_projection_blocked')
        if 'metrics' in e:
            m=e['metrics']
            if not cfg or cfg.runtime_profile not in ('m4-local','m4-server-readonly') or not isinstance(m,dict) or set(m)-METRIC_FIELDS.get(e.get('tool'),set()):raise ValueError('security_projection_blocked')
            import math
            if any(not (k=='cpu_percent' and v is None) and (type(v) not in (int,float) or not math.isfinite(v) or v<0) for k,v in m.items()):raise ValueError('security_projection_blocked')
            if e.get('tool')=='linux.get_cpu_usage' and m.get('cpu_percent') is not None and m['cpu_percent']>100:raise ValueError('security_projection_blocked')
        for a in e.get('assets',[]):
            if not isinstance(a,dict) or set(a)-{'asset_id','kind','parent_id','version','criticality','enabled'}:
                raise ValueError('security_projection_blocked')
    for a in payload['actions']:
        if not isinstance(a,dict) or set(a)!={'status'} or a['status'] not in ('started','succeeded','failed','execution_uncertain'):
            raise ValueError('security_projection_blocked')
    if 'inspection' in payload:
        inspection=payload['inspection']
        if not isinstance(inspection,dict) or set(inspection)!={'version','items','completeness'} or inspection['version']!='sec-m3-report/v1' or inspection['completeness']!='limited_project_checks':
            raise ValueError('security_projection_blocked')
        if not isinstance(inspection['items'],list) or len(inspection['items'])>40: raise ValueError('security_projection_blocked')
        statuses={'passed','finding','inconclusive','not_checked'}
        reasons={'insufficient_evidence','collection_failed','asset_version_mismatch','time_unknown','historical_evidence','stale_or_future_evidence','empty_or_truncated','observed_abnormal_event','filtered_events_not_full_audit','incomplete_evidence','container_not_running_or_unhealthy','registered_container_running_healthy','health_not_sufficient','service_failed','registered_services_active','service_state_inconclusive','database_unreachable','registered_database_reachable','capability_unavailable','inspection_interrupted'}
        rules={None,'observation_only','operational_event_observed','container_running_and_health','registered_service_state','database_reachability'}
        for item in inspection['items']:
            if not isinstance(item,dict) or set(item)!={'status','reason','rule','evidence_refs','asset_id','asset_version'}: raise ValueError('security_projection_blocked')
            if item['status'] not in statuses or item['reason'] not in reasons or item['rule'] not in rules: raise ValueError('security_projection_blocked')
            if item['asset_id'] is not None: contract.safe_text(item['asset_id']); __import__('ops_contract').asset_id(item['asset_id'])
            if item['asset_version'] is not None and (type(item['asset_version']) is not int or item['asset_version']<1): raise ValueError('security_projection_blocked')
            if not isinstance(item['evidence_refs'],list) or any(ref not in {e.get('evidence_ref') for e in payload['evidence']} for ref in item['evidence_refs']): raise ValueError('security_projection_blocked')
    contract.safe_text(contract.canonical(payload).decode())
    return payload


def licensed_summary(report, cfg=None):
    """Only fixed scalar state/aliases/hashes; no user request, model prose or raw tool body."""
    rows = []
    for e in report.get('infrastructure_evidence', []):
        value={k: e[k] for k in ('evidence_ref', 'tool', 'asset_id', 'asset_version', 'status',
                                      'complete', 'truncated', 'collected_at', 'sampled_at') if k in e}
        calls=[c for c in report.get('tool_calls',[]) if c.get('call_id')==e.get('call_id')]
        if calls:
            data=calls[-1].get('result',{})
            value['state']={k:data[k] for k in ('running','health','state','image_digest') if k in data}
            if cfg and cfg.runtime_profile in ('m4-local','m4-server-readonly') and e.get('status')=='ok':
                fields=METRIC_FIELDS.get(e.get('tool'),set())
                metrics={k:data[k] for k in fields if k in data}
                if e.get('tool')=='linux.get_disk_status':
                    rows_fs=data.get('filesystems',[])
                    metrics={'filesystem_count':len(rows_fs),'filesystems_total_bytes':sum(f['total_bytes'] for f in rows_fs),'filesystems_available_bytes':sum(f['available_bytes'] for f in rows_fs)}
                elif e.get('tool')=='linux.list_services':
                    services=data.get('list',[])
                    metrics={'registered_service_count':len(services),'active_service_count':sum(s.get('ActiveState')=='active' for s in services),'failed_service_count':sum(s.get('ActiveState')=='failed' for s in services)}
                elif e.get('tool')=='docker.list_containers':
                    containers=data.get('list',[])
                    metrics={'registered_container_count':len(containers),'running_container_count':sum(c.get('running') is True for c in containers)}
                if metrics:value['metrics']=metrics
            if e.get('tool')=='assets.list':
                from .runtime_profile import environments
                if any(a.get('environment') not in environments(cfg) for a in data.get('list',[])):
                    raise ValueError('security_projection_blocked')
                value['assets']=[{k:a[k] for k in ('asset_id','kind','parent_id','version','criticality','enabled') if k in a} for a in data.get('list',[])]
        rows.append(value)
    payload={'task_status':report['status'],'evidence':rows,'actions':[{'status':a['status']} for a in report.get('execution_attempts',[])]}
    if report.get('business_quality'):
        from .business_quality import quality_summary
        payload['business_quality'] = quality_summary(report['business_quality'])
    if report.get('inspection_report'):
        payload['inspection']={'version':'sec-m3-report/v1','completeness':'limited_project_checks','items':[{k:i[k] for k in ('status','reason','rule','evidence_refs','asset_id','asset_version')} for i in report['inspection_report']['items']]}
    return validate_payload(payload, cfg)


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
        self.cfg = kwargs.pop("runtime_config", None)
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
            allowed = ('sec_m2_acceptance_test', 'sec_m2_regression_test', 'sec_m3_acceptance_test', 'sec_m3_regression_test')
            if self.cfg and self.cfg.runtime_profile == 'm4-local': allowed = ('sec_m4_acceptance_test', 'sec_m4_regression_test')
            if self.cfg and self.cfg.runtime_profile == 'm4-server-readonly': allowed = ('ma_ops',)
            if await self.pool.fetchval('SELECT current_database()') not in allowed:
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
            if a['environment'] not in environments(self.cfg): raise ControlFailure('security_projection_blocked')
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
            asset=asset, policy_version=task_policy(task, self.cfg)[0], scope_version=task_policy(task, self.cfg)[1],
            normalized_intent=material['intent'], evidence=material['evidence'], egress=material.get('egress'),
            deadline=material['deadline'])
        contract.projection(request, (self.service_token, self.request_key, self.result_key))
        return request


class SecurityGates:
    def __init__(self, cfg, store, client):
        self.cfg, self.store = cfg, store
        self.controller = RuntimeController(store.pool, client, cfg.security_service_token,
            cfg.security_request_key, cfg.security_result_key, url=cfg.security_url, model='deepseek-v4.1-flash' if cfg.runtime_profile == 'm4-server-readonly' else 'deepseek-v4-flash-0731', runtime_config=cfg)

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

    async def require(self, executor, row, epoch, step, name, args, jwt, result, kind, plan=None, payload=None, receiver='operation', facts=None, history=False, opinion_only=False):
        """No network request until identity, scope, safe material and durable audit pass."""
        actor = await executor.identity(jwt)
        if actor['user_id'] != row['user_id'] or actor['role'] not in ('admin', 'operator'):
            raise ValueError('security_invalid')
        current = await self.store.get(row['task_id'], row['user_id'])
        if current['execution_epoch'] != epoch: raise ValueError('security_review_stale')
        policy,scope=task_policy(current, self.cfg)
        if opinion_only and (kind!='inspection_report' or scope not in (contract.INSPECTION_SCOPE, contract.M4_LOCAL_SCOPE, contract.M4_SERVER_SCOPE)): raise ValueError('security_invalid')
        if not history: await self.store.check(row['task_id'], epoch)
        asset = await self.store.resolve_asset(args['asset_id'], name) if args.get('asset_id') else None
        if asset and asset['environment'] not in environments(self.cfg):
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
            'intent': {'sensitive_read': '审查本项目登记许可范围内目标的固定安全字段读取',
                       'write_plan': '审查本机隔离无状态演练容器重启提案，随后人工审批',
                       'write_preflight': '审查已人工批准的本机演练容器保存计划',
                       'post_execution': '复核本机演练已保存动作事实，禁止自动补写',
                       'data_egress': '审查许可内本项目许可内状态摘要发往现有网关',
                       'inspection_report':'复核本轮限定项目只读巡检报告，不改事实或自动修复'}[kind],
            'evidence': evidence, 'deadline': deadline.isoformat()}
        if kind in ('data_egress','inspection_report'):
            validate_payload(payload, self.cfg)
            if kind=='inspection_report' and 'inspection' not in payload: raise ValueError('security_projection_blocked')
            bindings=[]
            for e in payload['evidence']:
                scope_assets=list(e.get('assets',[]))
                if e.get('asset_id'): scope_assets.append({'asset_id':e['asset_id'],'version':e['asset_version']})
                for a in scope_assets:
                    current_asset=await self.store.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',a['asset_id'])
                    if not current_asset or not current_asset['enabled'] or not current_asset['verified_at'] or current_asset['version']!=a['version'] or current_asset['environment'] not in environments(self.cfg):
                        raise ValueError('security_projection_blocked')
                    bindings.append({k:current_asset[k] for k in ('asset_id','version','identity_hash')})
            for item in payload.get('inspection',{}).get('items',[]):
                if item['asset_id']:
                    a=await self.store.pool.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1',item['asset_id'])
                    if not a or not a['enabled'] or not a['verified_at'] or a['version']!=item['asset_version'] or a['environment'] not in environments(self.cfg): raise ValueError('security_review_stale')
                    bindings.append({k:a[k] for k in ('asset_id','version','identity_hash')})
            material['registry_bindings']=bindings
            contract.safe_text(contract.canonical(payload).decode(), (jwt, *self.cfg_secrets()))
            if len(contract.canonical(payload)) > 32768: raise ValueError('security_projection_blocked')
            values['egress_permitted'] = True
            values['registry_verified'] = True
            values['payload_bound'] = True
            values['material_hash'] = digest(payload)
            evidence[0]['facts'] = [{'key': k, 'value': v} for k, v in values.items()]
            material['egress'] = {'destination': 'existing-gateway', 'scope_version': scope,
                'receiver': receiver, 'model': 'deepseek-v4.1-flash' if receiver == 'supervisor' or scope == contract.M4_SERVER_SCOPE else 'deepseek-v4-flash-0731',
                'fields': ['normalized_intent', 'tool', 'asset', 'evidence', 'policy_version'], 'payload_hash': digest(payload)}
            if kind=='inspection_report': material.pop('egress',None)
            if kind=='inspection_report' and scope in (contract.INSPECTION_SCOPE, contract.M4_LOCAL_SCOPE, contract.M4_SERVER_SCOPE) and 'inspection' in payload:
                items=payload['inspection']['items']
                evidence[0]['facts'] += [{'key':'checked_items','value':sum(i['status']!='not_checked' for i in items)}, {'key':'not_checked_items','value':sum(i['status']=='not_checked' for i in items)}, {'key':'coverage_limited','value':True}]
                for index,item in enumerate(items):
                    evidence.append({'ref':'C'+str(index+1),'source':'program','collected_at':now.isoformat(),'complete':True,'filtered':True,'truncated':False,
                        'facts':[{'key':'inspection_status','value':item['status']},{'key':'inspection_reason','value':item['reason']},{'key':'inspection_rule','value':item['rule']}]})
                # Bounded report <=16 tools plus category/asset missing items; summarize if needed without dropping coverage.
                if len(evidence)>20: raise ValueError('inspection_scope_too_large')
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
        if record['decision']['decision'] != 'allow' and not opinion_only:
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
        task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1',tid)
        policy,scope=task_policy(task, self.cfg)
        if request['policy_version'] != policy or request['scope_version'] != scope or material['args_hash'] != args_hash:
            raise ValueError('security_review_stale')
        if plan is not None and material['plan_hash'] != digest(plan): raise ValueError('security_review_stale')
        if plan is not None and plan.get('security_policy')!=policy:raise ValueError('security_review_stale')
        if request.get('asset'):
            a = await c.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1 FOR SHARE', request['asset']['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version'] != request['asset']['version'] or a['identity_hash'] != request['asset']['identity_hash']:
                raise ValueError('security_review_stale')
        for binding in material.get('registry_bindings',[]):
            a=await c.fetchrow('SELECT * FROM ma_assets WHERE asset_id=$1 FOR SHARE',binding['asset_id'])
            if not a or not a['enabled'] or not a['verified_at'] or a['version']!=binding['version'] or a['identity_hash']!=binding['identity_hash']:
                raise ValueError('security_review_stale')
        return row['review_id']
