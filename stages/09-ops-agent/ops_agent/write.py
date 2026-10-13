"""R2 planning stops at approval. Approved execution is entirely program controlled."""
import json
import time
from datetime import datetime,timezone
from pathlib import Path
from ops_contract import digest
from ops_write_contract import TOOL, restart_args, explicit_restart, proof, matches, sign

SCHEMA={'type':'function','function':{'name':'docker__restart_drill_container',
    'description':'仅在用户明确要求重启且任务允许R2时，为登记的本机隔离无状态演练容器生成管理员审批。此调用不执行重启；不要用于服务器慢的诊断或建议。先用assets__list定位精确asset_id。',
    'parameters':{'type':'object','properties':{'asset_id':{'type':'string'}},'required':['asset_id'],'additionalProperties':False}}}
MANIFEST={'name':TOOL,'version':'1.0','category':'docker','risk_class':'R2','required_roles':['admin'],
          'approval_policy':'owner_one_shot','idempotent':False,'target_kinds':['container'],
          'timeout_sec':15,'args_schema':SCHEMA['function']['parameters'],'enabled_by_default':False,
          'output_fields':['before','after','action_confirmed']}


class ApprovalPending(Exception): pass


async def snapshot(executor,aid,row=None,epoch=None,step=None):
    body={'asset_id':aid}
    if executor.store.security_gates:
        if row is None: raise ValueError('security_invalid')
        from .security import sign as sign_context
        now=int(time.time())
        body['gate']=sign_context({'aud':'ma-security-snapshot','kid':'ma-v1','iat':now,'exp':now+30,
            'task_id':str(row['task_id']),'user_id':row['user_id'],'execution_epoch':epoch,'step_no':step,'asset_id':aid},executor.cfg.executor_grant_key)
    r=await executor.client.post(executor.cfg.executor_url+'/internal/ma-exec/v2/snapshot',json=body,headers={'X-MA-Executor-Token':executor.cfg.executor_token},timeout=executor.cfg.tool_timeout)
    if r.status_code!=200: raise ValueError('asset_version_changed')
    value=r.json();proof(value['snapshot'])
    if digest(value['policy'])!=value['snapshot']['policy_version']: raise ValueError('invalid_result')
    return value


def audit(executor,record):
    executor.cfg.audit_dir.mkdir(parents=True,exist_ok=True)
    path=executor.cfg.audit_dir/'write-preflight.jsonl'
    # flush + fsync precede approval consumption and any external mutation.
    with path.open('a',encoding='utf-8') as f:
        f.write(executor.cfg.redact(json.dumps(record,ensure_ascii=False,default=str))+'\n');f.flush();__import__('os').fsync(f.fileno())


async def prepare(executor,row,epoch,step,arguments,jwt,result):
    cfg=executor.cfg;args=restart_args(arguments)
    env=json.loads(row['envelope']) if isinstance(row['envelope'],str) else row['envelope']
    if not cfg.enabled or not cfg.write_enabled or env.get('operation')!='restart_drill_container' or env['constraints']['max_risk_class']!='R2' or not explicit_restart(env['user_request']): raise ValueError('guard_blocked')
    actor=await executor.identity(jwt)
    if actor['user_id']!=row['user_id'] or actor['role']!='admin': raise PermissionError('permission_denied')
    asset=await executor.store.resolve_asset(args['asset_id'],TOOL)
    if asset['criticality']!='drill' or asset['environment'] not in ('acceptance','fixture'): raise ValueError('guard_blocked')
    # Persist the ordinary R0 inspect independently before proposing the write step.
    read=await executor.call(row,epoch,step,'docker.inspect_container',args,jwt,result)
    if 'error' in read: raise ValueError('required_tool_failed')
    state=await snapshot(executor,args['asset_id'],row,epoch,step)
    if state['snapshot']['health']!='healthy' or not state['snapshot']['running']: raise ValueError('drill_not_healthy')
    audit(executor,{'event':'approval_prepared','task_id':str(row['task_id']),'execution_epoch':epoch,'snapshot':state['snapshot']})
    result.setdefault('operation_evidence',[]).append({'phase':'preflight','snapshot':state['snapshot']})
    if executor.store.security_gates:
        from .approvals import saved_plan
        from .runtime_reviews import contract
        await executor.store.security_gates.require(executor,row,epoch,step+1,TOOL,args,jwt,result,'write_plan',
            plan=saved_plan(args,state['snapshot'],contract.RUNTIME_POLICY),facts={'running':state['snapshot']['running'],'health':state['snapshot']['health']})
    await executor.store.prepare_approval(row['task_id'],epoch,step+1,args,state['snapshot'],state['policy'],result)
    raise ApprovalPending()


async def execute(executor,row,epoch,approval,jwt,result):
    cfg=executor.cfg;tid=row['task_id'];plan=approval['plan'];cid=None
    if not cfg.enabled or not cfg.write_enabled: raise ValueError('write_disabled')
    if executor.store.security_required:
        from .runtime_reviews import contract
        if plan.get('security_policy')!=contract.RUNTIME_POLICY:raise ValueError('approval_invalidated')
    actor=await executor.identity(jwt)
    if actor['user_id']!=row['user_id'] or actor['role']!='admin': raise PermissionError('permission_denied')
    if executor.store.security_gates:
        await executor.store.security_gates.require(executor,row,epoch,approval['step_no']-1,'docker.inspect_container',plan['args'],jwt,result,'sensitive_read')
    state=await snapshot(executor,plan['args']['asset_id'],row,epoch,approval['step_no']-1)
    if not matches(plan['snapshot'],state['snapshot']) or state['snapshot']['health']!='healthy': raise ValueError('approval_invalidated')
    if executor.store.security_gates:
        await executor.store.security_gates.require(executor,row,epoch,approval['step_no'],TOOL,plan['args'],jwt,result,'write_preflight',plan=plan,
            facts={'approval_status':'approved','approval_valid':approval['expires_at']>datetime.now(timezone.utc),
                   'approval_current_epoch':epoch,'approval_expires_at':approval['expires_at'].isoformat(),
                   'plan_unchanged':True,'policy_current':state['snapshot']['policy_version']==plan['snapshot']['policy_version']})
    await executor.store.check(tid,epoch)
    audit(executor,{'event':'write_attempt_ready','task_id':str(tid),'approval_id':str(approval['approval_id']),'snapshot':state['snapshot']})
    cid=await executor.store.begin_write(tid,epoch,approval)
    now=int(time.time());current=await executor.store.get(tid)
    g={'aud':'ma-drill-executor','kid':'ma-write-v1','action':'restart','iat':now,'exp':now+30,
       'task_id':str(tid),'trace_id':str(row['trace_id']),'execution_epoch':epoch,'step_no':approval['step_no'],
       'call_id':str(cid),'user_id':row['user_id'],'role':actor['role'],'args_hash':plan['args_hash'],
       'deadline':current['deadline'].timestamp(),'approval_id':str(approval['approval_id']),
       **{k:plan['snapshot'][k] for k in ('asset_id','asset_version','registry_hash','container_id','image_digest','config_hash','policy_version')}}
    data={};error=None;start=time.monotonic()
    try:
        r=await executor.client.post(cfg.executor_url+'/internal/ma-exec/v2/writes',json={'grant':sign(g,cfg.executor_write_key)},headers={'X-MA-Executor-Token':cfg.executor_token},timeout=cfg.tool_timeout)
        if r.status_code!=200:
            consumed=await executor.store.pool.fetchval('SELECT consumed_at FROM ma_approvals WHERE approval_id=$1',approval['approval_id'])
            code='execution_uncertain' if consumed else 'approval_invalidated'
            error={'code':code,'message':code,'retryable':False}
        else:
            reply=r.json()
            if set(reply)!={'status','data','error'} or reply['status'] not in ('ok','error'): raise ValueError('invalid_result')
            data=reply['data'];error=reply['error']
            if error:
                data={};error={'code':'execution_uncertain','message':'execution_uncertain','retryable':False}
            else:
                if not isinstance(data,dict) or set(data)!={'status','before','after','action_confirmed'} or data['status']!='ok' or data.get('action_confirmed') is not True or not matches(g,proof(data['before'])) or not matches(g,proof(data['after'])) or data['after']['health']!='healthy' or data['before']['started_at']==data['after']['started_at']: raise ValueError('invalid_result')
    except Exception:
        data={}
        error={'code':'execution_uncertain','message':'动作结果不确定；禁止自动重试，请另行只读核对。','retryable':False}
    evidence={'phase':'execution','call_id':str(cid),'approval_id':str(approval['approval_id']),'data':data,'error':error}
    result.setdefault('operation_evidence',[]).append(evidence)
    result['tool_calls'].append({'name':TOOL,'arguments':plan['args'],'status':'error' if error else 'ok','call_id':str(cid),'step_no':approval['step_no'],'collected_at':datetime.now(timezone.utc).isoformat(),**({'error':error['message']} if error else {'result':data})})
    if error: result['error']=error
    try:
        # Evidence and attempt outcome commit together. A missing commit remains a started attempt.
        async with executor.store.pool.acquire() as c,c.transaction():
            task=await c.fetchrow('SELECT * FROM ma_tasks WHERE task_id=$1 FOR UPDATE',tid)
            if task['execution_epoch']!=epoch or task['status']!='executing' or task['lease_until']<=datetime.now(timezone.utc): raise ValueError('execution_context_lost')
            status='failed' if error else 'succeeded'
            encoded=json.dumps(data,ensure_ascii=False);err=json.dumps(error,ensure_ascii=False) if error else None
            await c.execute('UPDATE ma_tool_calls SET status=$2,data=$3::jsonb,error=$4::jsonb,duration_ms=$5,finished_at=now() WHERE call_id=$1',cid,status,encoded,err,int((time.monotonic()-start)*1000))
            await c.execute('UPDATE ma_task_steps SET status=$3,result=$4::jsonb,error=$5::jsonb,finished_at=now() WHERE task_id=$1 AND step_no=$2',tid,approval['step_no'],status,encoded,err)
            await c.execute("UPDATE ma_execution_attempts SET status=$2,evidence=$3::jsonb,finished_at=now() WHERE call_id=$1 AND status='started'",cid,'execution_uncertain' if error and error['code']=='execution_uncertain' else status,json.dumps(evidence,ensure_ascii=False))
            await c.execute('UPDATE ma_tasks SET result=$2::jsonb WHERE task_id=$1',tid,json.dumps(result,ensure_ascii=False))
    except Exception:
        # Leave the started attempt for the authoritative recovery path. Never dispatch again.
        await executor.store.recover(startup=True,task_id=tid)
        return
    task=await executor.store.get(tid)
    if executor.store.security_gates:
        try:
            await executor.store.security_gates.require(executor,row,epoch,approval['step_no'],TOOL,plan['args'],jwt,result,'post_execution',plan=plan,
                facts={'execution_status':'execution_uncertain' if error else 'succeeded','approval_status':'approved','approval_consumed':True},history=True)
        except Exception:
            result.setdefault('warnings',[]).append('安全后审未通过或不可用；已保存的动作事实不变，禁止自动补写。')
    result.setdefault('warnings',[]).append('取消不代表已回滚；操作前后状态和原审批保留。') if task['cancel_requested'] else None
    await executor.store.transition(tid,epoch,'failed' if error else 'cancelled' if task['cancel_requested'] else 'succeeded',result)
