"""SEC-M3 fixed read-only inspection. Models never select targets or change facts."""
import json
import re
from datetime import datetime, timezone
from ops_contract import TOOLS

CATEGORIES = ('host_services', 'containers', 'database', 'permissions_exposure', 'credentials_egress', 'audit_recovery')
ORDER = tuple(TOOLS)
ABNORMAL_EVENTS = frozenset({'subscribe_failed','telemetry_rejected','unregistered_device','device_lookup_failed','station_mismatch','queue_write_failed','queue_read_failed','queue_decode_failed','tdengine_write_failed','queue_transaction_failed','queue_delete_failed','queue_commit_failed','business_notification_worker_failed','business_notification_retry','interpretation_scan_failed','monitor_consumer_unavailable','monitor_payload_invalid','monitor_persist_failed','interpretation_claim_failed','prediction_devices_unavailable','prediction_skipped','backend_subscribe_failed','backend_rejected','chat_admission_release_failed','service_failed','connection_failed','health_check_failed'})

def stamp(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(timezone.utc) if dt.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None

def select_assets(assets, envelope, cfg=None):
    """Resolve registry aliases only. Missing/ambiguous scope never probes a host."""
    indexed = {a['asset_id']: a for a in assets}
    targets = envelope.get('targets', [])
    explicit_ids = [t['asset_id'] for t in targets if t.get('asset_id')]
    text = envelope['user_request']+' '+' '.join(t.get('description') or '' for t in targets)
    normalized_text=re.sub(r'\s+','',text)
    if explicit_ids:
        if any(aid not in indexed for aid in explicit_ids): raise ValueError('asset_not_found')
        chosen = [indexed[aid] for aid in dict.fromkeys(explicit_ids)]
    else:
        # Match whole aliases; a partial asset id cannot silently select another target.
        matches = [a for a in assets if any(re.search(r'(?<![\w-])'+re.escape(s)+r'(?![\w-])', text, re.I) if s==a['asset_id'] else re.sub(r'\s+','',s) in normalized_text for s in (a['asset_id'], a['display_name']))]
        if len(matches) > 1: raise ValueError('target_ambiguous')
        if matches:
            chosen = matches
        elif re.search(r'本项目|本平台|本轮登记|当前项目|整个项目|\bthis project\b', text, re.I):
            chosen = list(assets)
        else:
            raise ValueError('target_ambiguous')
    expanded = {a['asset_id']: a for a in chosen}
    for a in chosen:
        if a['kind']=='host':
            expanded.update({b['asset_id']: b for b in assets if b.get('parent_id')==a['asset_id']})
    for a in expanded.values():
        if not a['enabled'] or not a['verified_at']: raise ValueError('asset_not_verified')
        from .runtime_profile import environments
        if a['environment'] not in environments(cfg): raise ValueError('inspection_scope_blocked')
        if a.get('project_scope', 'powersystem') != 'powersystem': raise ValueError('inspection_scope_blocked')
        if a['parent_id']:
            parent = indexed.get(a['parent_id'])
            if not parent or not parent['enabled'] or not parent['verified_at'] or parent['environment'] not in environments(cfg):
                raise ValueError('asset_not_verified')
    return sorted(expanded.values(), key=lambda a:a['asset_id'])

def make_plan(assets, max_calls):
    missing_count=len(report_for(assets,datetime.now(timezone.utc).isoformat())['items'])
    plan = [(a, tool) for a in assets for tool in ORDER if tool in a['capabilities']]
    if len(plan)+1 > max_calls or len(plan)+missing_count>19: raise ValueError('inspection_scope_too_large')
    return plan

def category(tool):
    return 'containers' if tool.startswith('docker.') else 'database' if tool.startswith('postgresql.') else 'host_services'

def evaluate(tool, data, evidence, created_at, asset):
    item = {'check_id':asset['asset_id']+':'+tool, 'category':category(tool), 'asset_id':asset['asset_id'], 'asset_version':asset['version'], 'tool':tool,
            'status':'inconclusive', 'reason':'insufficient_evidence', 'rule':'observation_only', 'evidence_refs':[evidence['evidence_ref']],
            'sampled_at':evidence.get('sampled_at'), 'collected_at':evidence.get('collected_at'), 'vantage':evidence.get('vantage'), 'historical':False}
    collected, sampled, start = stamp(item['collected_at']), stamp(item['sampled_at']), stamp(created_at)
    if evidence.get('status')!='ok': item['reason']='collection_failed'; return item
    if evidence.get('asset_id')!=asset['asset_id'] or evidence.get('asset_version')!=asset['version']:
        item['reason']='asset_version_mismatch'; return item
    if not collected or not sampled or not start: item['reason']='time_unknown'; return item
    if collected < start: item.update(reason='historical_evidence', historical=True); return item
    age = (collected-sampled).total_seconds()
    if age>300 or age < -60: item['reason']='stale_or_future_evidence'; return item
    if not data or evidence.get('truncated') or data.get('truncated'): item['reason']='empty_or_truncated'; return item
    if tool.endswith('_logs'):
        events = data.get('events', [])
        if any(e.get('event') in ABNORMAL_EVENTS for e in events):
            item.update(status='finding', reason='observed_abnormal_event', rule='operational_event_observed')
        else: item['reason']='filtered_events_not_full_audit'
        return item
    if evidence.get('complete') is not True or data.get('complete') is not True:
        item['reason']='incomplete_evidence'; return item
    if tool=='docker.inspect_container':
        item['rule']='container_running_and_health'
        if data.get('running') is False or data.get('health')=='unhealthy':
            item.update(status='finding', reason='container_not_running_or_unhealthy')
        elif data.get('running') is True and data.get('state')=='running' and data.get('health')=='healthy':
            item.update(status='passed', reason='registered_container_running_healthy')
        else: item['reason']='health_not_sufficient'
    elif tool=='linux.list_services':
        item['rule']='registered_service_state'
        rows=data.get('list', [])
        if any(r.get('ActiveState')=='failed' or r.get('LoadState') in ('error','not-found') for r in rows):
            item.update(status='finding', reason='service_failed')
        elif rows and all(r.get('ActiveState')=='active' and r.get('LoadState')=='loaded' for r in rows):
            item.update(status='passed', reason='registered_services_active')
        else: item['reason']='service_state_inconclusive'
    elif tool=='postgresql.get_db_status':
        item['rule']='database_reachability'
        if data.get('reachable') is False: item.update(status='finding', reason='database_unreachable')
        elif data.get('reachable') is True: item.update(status='passed', reason='registered_database_reachable')
    return item

def report_for(assets, created_at):
    report={'version':'sec-m3-report/v1','requested_scope':[a['asset_id'] for a in assets],
            'checked_scope':[], 'items':[], 'confirmed_findings':[], 'hypotheses':[], 'unchecked_items':[],
            'recommendations':[], 'completeness':'limited_project_checks', 'review_status':'not_started', 'created_at':str(created_at)}
    represented={category(t) for a in assets for t in ORDER if TOOLS[t][0]==a['kind']}
    for cat in CATEGORIES:
        if cat not in represented:
            report['items'].append({'check_id':cat, 'category':cat, 'asset_id':None, 'asset_version':None, 'tool':None,
                'status':'not_checked','reason':'capability_unavailable','rule':None,'evidence_refs':[], 'sampled_at':None,'collected_at':None,'vantage':None,'historical':False})
    for a in assets:
        for tool in ORDER:
            if TOOLS[tool][0]!=a['kind'] or tool in a['capabilities']: continue
            report['items'].append({'check_id':a['asset_id']+':'+tool,'category':category(tool),
                'asset_id':a['asset_id'],'asset_version':a['version'],'tool':tool,'status':'not_checked','reason':'capability_unavailable',
                'rule':None,'evidence_refs':[], 'sampled_at':None,'collected_at':None,'vantage':None,'historical':False})
    return finalize(report)

def finalize(report):
    report['checked_scope']=sorted({i['asset_id'] for i in report['items'] if i['tool'] and i['evidence_refs']})
    report['confirmed_findings']=[dict(i) for i in report['items'] if i['status']=='finding']
    report['unchecked_items']=[dict(i) for i in report['items'] if i['status']=='not_checked']
    report['recommendations']=['请人工核对发现及未检查项；新增采集能力或实际修复须另行立项与授权。']
    return report

async def run(worker, row, epoch, envelope, jwt, result, call):
    catalog=await call('assets.list', {})
    assets=select_assets(catalog['list'], envelope, worker.cfg)
    plan=make_plan(assets, worker.cfg.max_calls)
    if not worker.store.security_gates: raise ValueError('security_unavailable')
    report=report_for(assets, row['created_at'].isoformat())
    result['inspection_report']=report
    result['diagnosis']={'summary':'本轮项目只读巡检：程序清单及安全复核分别展示，未检查项不代表通过。'}
    # Fix the complete selected scope before any remote read. Every read rechecks it.
    versions={a['asset_id']:a['version'] for a in assets}
    async def check_scope():
        current=(await worker.store.list_assets())['list']
        indexed={a['asset_id']:a for a in current}
        for aid, version in versions.items():
            a=indexed.get(aid)
            if not a or not a['enabled'] or not a['verified_at'] or a['version']!=version:
                raise ValueError('asset_version_changed')
        await worker.store.check(row['task_id'],epoch)
    try:
        for asset, tool in plan:
            await check_scope()
            data=await call(tool, {'asset_id':asset['asset_id']})
            evidence=result['infrastructure_evidence'][-1]
            item=evaluate(tool,data,evidence,row['created_at'].isoformat(),asset)
            report['items'].append(item); finalize(report)
            if 'error' in data: raise ValueError('required_tool_failed')
        await check_scope()
        finalize(report)
        report['review_status']='pending'
        await worker.store.transition(row['task_id'],epoch,'executing',result)
        # Last actual step binds the opinion; an empty-capability scope uses assets.list.
        step=await worker.store.pool.fetchrow('SELECT * FROM ma_task_steps WHERE task_id=$1 ORDER BY step_no DESC LIMIT 1',row['task_id'])
        from .runtime_reviews import licensed_summary
        payload=licensed_summary({'status':'executing','task_kind':'security_inspection', **result}, worker.cfg)
        rid=await worker.store.security_gates.require(worker.executor,row,epoch,step['step_no'],step['tool'],json.loads(step['args']),jwt,result,
            'inspection_report',payload=payload, opinion_only=True)
        review=await worker.store.security_gates.controller.get(rid,row['user_id'])
        report['review_status']=review['state']
        report['hypotheses']=[{'review_id':str(rid),'decision':review['decision']['decision'],'reason':review['decision']['reason'],'evidence_refs':review['decision']['evidence_refs']}]
        await check_scope()
    except Exception:
        if report['review_status']=='pending': report['review_status']='unavailable'
        known={i['check_id'] for i in report['items']}
        for asset,tool in plan:
            if asset['asset_id']+':'+tool not in known:
                report['items'].append({'check_id':asset['asset_id']+':'+tool,'category':category(tool),'asset_id':asset['asset_id'],'asset_version':asset['version'],
                    'tool':tool,'status':'not_checked','reason':'inspection_interrupted','rule':None,'evidence_refs':[], 'sampled_at':None,'collected_at':None,'vantage':None,'historical':False})
        finalize(report)
        raise
