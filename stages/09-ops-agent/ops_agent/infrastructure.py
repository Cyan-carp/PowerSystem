"""Task-side R0 scheduler. SSH/DB credentials exist only in the model-free adapter."""
import asyncio
import json
import time
from datetime import datetime,timezone
import httpx
from pydantic import BaseModel,ConfigDict,Field
from ops_contract import TOOLS,OUTPUT_FIELDS,COMMON_OUTPUT,schema,validate_args,validate_result,sign_grant,digest,ASSET_PATTERN


class AssetQuery(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: __import__('typing').Literal['host','service','container','database'] | None=None
    parent_id: str | None=Field(default=None,pattern='^'+ASSET_PATTERN+'$')
    keyword: str | None=Field(default=None,max_length=100)


def returns_schema(name):
    """Describe the fixed successful evidence projection, not arbitrary JSON."""
    fields=COMMON_OUTPUT|OUTPUT_FIELDS[name]
    strings={'sampled_at','collected_at','state','image_digest','health','started_at','finished_at','server_version','filter_policy'}
    booleans={'truncated','complete','running','reachable','in_recovery'}
    properties={field:({'type':'string'} if field in strings else {'type':'boolean'} if field in booleans else {'type':'array','items':{'type':'object'}} if field in {'events','filesystems','list'} else {'type':['number','null']} if field=='cpu_percent' else {'type':'number','minimum':0}) for field in fields}
    properties['vantage']={'type':'string','enum':['container' if name.startswith('docker.') and name!='docker.list_containers' else 'database' if name.startswith('postgresql.') else 'host']}
    for field in ('sampled_at','collected_at'):properties[field]['format']='date-time'
    return {'type':'object','properties':properties,'required':sorted(fields),'additionalProperties':False}


MANIFEST={name:{'name':name,'version':'1.0','category':name.split('.')[0],'risk_class':'R0',
                   'required_roles':['operator','admin'],'approval_policy':'none','idempotent':True,
                   'enabled':True,'target_kinds':[kind],'timeout_sec':15,'args_schema':schema(name),
                   'returns_schema':returns_schema(name),'output_fields':sorted(COMMON_OUTPUT|OUTPUT_FIELDS[name])}
          for name,(kind,_) in TOOLS.items()}
MANIFEST['assets.list']={'name':'assets.list','version':'1.0','category':'assets','risk_class':'R0',
    'required_roles':['operator','admin'],'approval_policy':'none','idempotent':True,'enabled':True,
    'target_kinds':[],'timeout_sec':15,'args_schema':AssetQuery.model_json_schema(),
    'returns_schema':{'type':'object','properties':{'list':{'type':'array','items':{'type':'object'}},'total':{'type':'integer','minimum':0},'complete':{'type':'boolean'},'slow_queries_available':{'type':'boolean'}},'required':['list','total','complete','slow_queries_available'],'additionalProperties':False},'output_fields':['list','total','complete','slow_queries_available']}
SCHEMAS=[{'type':'function','function':{'name':name.replace('.','__'),
    'description':description+'；先用assets__list取得准确asset_id，多候选先澄清，禁止猜目标。',
    'parameters':schema(name)}} for name,(_,description) in TOOLS.items()]
SCHEMAS.insert(0,{'type':'function','function':{'name':'assets__list','description':'读取项目登记资产别名、父子关系和可用能力，不探测主机。基础设施诊断先调用本工具定位；多个同名候选必须澄清，不猜。慢查询能力不可用。',
                                           'parameters':AssetQuery.model_json_schema()}})


async def execute(executor,row,epoch,step,name,arguments,jwt,result):
    cfg,store,client=executor.cfg,executor.store,executor.client
    if not cfg.infra_enabled: raise ValueError('capability_unavailable')
    try:
        args=AssetQuery.model_validate(arguments).model_dump(exclude_none=True) if name=='assets.list' else validate_args(name,arguments)
    except Exception: raise ValueError('invalid_args') from None
    args=json.loads(cfg.redact(json.dumps(args,ensure_ascii=False),(jwt,)))
    actor=await executor.identity(jwt)
    if actor['user_id']!=row['user_id'] or actor['role'] not in ('operator','admin'): raise PermissionError('permission_denied')
    asset=await store.resolve_asset(args['asset_id'],name) if name!='assets.list' else None
    if asset:
        # Multiple project hosts require an explicit unambiguous semantic target, never a model guess.
        hosts=(await store.list_assets(kind='host'))['list']
        hosts=[h for h in hosts if h['enabled'] and h['verified_at']]
        if len(hosts)>1:
            payload=json.loads(row['envelope']) if isinstance(row['envelope'],str) else row['envelope']
            text=(payload['user_request']+' '+' '.join(t.get('description') or '' for t in payload.get('targets',[]))).casefold()
            matches=[h for h in hosts if h['asset_id'].casefold() in text or h['display_name'].casefold() in text]
            root=asset['asset_id'] if asset['kind']=='host' else asset['parent_id']
            if len(matches)!=1 or matches[0]['asset_id']!=root: raise ValueError('target_ambiguous')
    if store.security_gates:
        from .runtime_reviews import SENSITIVE
        if name in SENSITIVE:
            await store.security_gates.require(executor,row,epoch,step,name,args,jwt,result,'sensitive_read')
    for attempt in range(2):
        current=await store.check(row['task_id'],epoch)
        if asset: asset=await store.resolve_asset(args['asset_id'],name)
        call_id=await store.begin_call(row['task_id'],epoch,step,name,args,row['trace_id'],asset=asset)
        stamp=datetime.now(timezone.utc).isoformat();started=time.monotonic();data={};error=None
        trace={'name':name,'arguments':args,'status':'error','collected_at':stamp,'call_id':str(call_id),'step_no':step}
        evidence={'call_id':str(call_id),'evidence_ref':'call:'+str(call_id),'tool':name,'tool_version':'1.0',
                  'asset_id':asset['asset_id'] if asset else None,'asset_version':asset['version'] if asset else None,
                  'asset_name':asset['display_name'] if asset else None,'status':'error','vantage':'registry' if asset is None else None,
                  'collected_at':stamp,'sampled_at':None,'truncated':False,'complete':False,'error':None}
        try:
            async with asyncio.timeout(cfg.tool_timeout):
                if name=='assets.list':
                    data=await store.list_assets(**args)
                else:
                    now=int(time.time())
                    grant={'aud':'ma-executor','kid':'ma-exec-v1','iat':now,'exp':now+30,
                           'task_id':str(row['task_id']),'trace_id':str(row['trace_id']),'execution_epoch':epoch,
                           'step_no':step,'call_id':str(call_id),'user_id':row['user_id'],'role':actor['role'],
                           'tool':name,'args_hash':digest(args),'asset_id':asset['asset_id'],'asset_version':asset['version'],
                           'registry_hash':asset['registry_hash'],'deadline':current['deadline'].timestamp()}
                    signed=sign_grant(grant,cfg.executor_grant_key)
                    reply=await client.post(cfg.executor_url+'/internal/ma-exec/v1/calls',
                        json={'protocol':'ma-exec/v1','grant':signed,'tool':name,'args':args},
                        headers={'X-MA-Executor-Token':cfg.executor_token},timeout=cfg.tool_timeout)
                    if reply.status_code!=200:
                        code='permission_denied' if reply.status_code in (401,403) else 'dispatch_replayed' if reply.status_code==409 else 'target_unreachable'
                        error={'code':code,'message':code,'retryable':reply.status_code>=500}
                    else:
                        parsed=validate_result(name,reply.json());data=parsed['data'];error=parsed['error']
                if not error:
                    data=json.loads(cfg.redact(json.dumps(data,ensure_ascii=False,default=str),(jwt,)))
                    if len(json.dumps(data,ensure_ascii=False).encode())>262144: raise ValueError('invalid_result')
                    trace.update(status='ok',result=data);evidence.update(status='ok',complete=data.get('complete',True))
                    for field in ('vantage','collected_at','sampled_at','truncated','filtered_lines','filter_policy'):
                        if field in data: evidence[field]=data[field]
                    if not data.get('complete',True): result['warnings'].append(name+': 过滤或截断的证据不代表完整日志，不据此认定没有异常。')
                    result['sources'].append({'id':'I'+str(call_id),'kind':'infrastructure','title':name,
                        'url':'/api/v1/agent/tasks/'+str(row['task_id']),'collected_at':evidence['collected_at']})
        except (httpx.HTTPError,TimeoutError):
            error={'code':'target_unreachable','message':'基础设施采集连接失败或超时','retryable':True}
        except (ValueError,KeyError,TypeError):
            error={'code':'invalid_result','message':'采集结果不符合安全输出契约','retryable':False}
        if error:
            trace['error']=error['message'];evidence['error']=error
            result['warnings'].append(name+': '+error['message'])
        result['tool_calls'].append(trace);result.setdefault('infrastructure_evidence',[]).append(evidence)
        await store.end_call(row['task_id'],epoch,step,call_id,data,error,int((time.monotonic()-started)*1000),result)
        if not error: return {**data,'call_id':str(call_id),'evidence_ref':evidence['evidence_ref']}
        if not error['retryable'] or attempt==1: return {'error':error['message'],'error_code':error['code']}
