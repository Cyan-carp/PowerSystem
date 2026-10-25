"""S07 isolated registered-service domain; no caller-selected transport or command."""
from typing import Literal
from pydantic import Field
from .business_contract import Strict, Decision, digest, sign_grant, canonical
from .business_contract import verify_grant as _verify
from uuid import UUID
import base64, hmac, hashlib, json, time

POLICY='v4-s07-local/v1'
PROTOCOL='ma-ops/v1'
CAPABILITIES={'service.diagnose':('R0','internal'),'service.logs':('R0','sensitive'),
              'service.evidence':('R0','sensitive'),'service.restart':('R2','internal')}
RECOVERY='重启不会撤销已发生的请求或数据变更；健康失败请人工核对登记版本、进程及日志，必要时按既有备份恢复流程处理。本动作不自动回滚、不重发。'
class ServiceArgs(Strict):
    asset_id:str=Field(pattern='^[a-z][a-z0-9_-]{0,79}$',strict=True)
class LogsArgs(ServiceArgs):
    limit:int=Field(default=100,ge=1,le=100,strict=True)
    since_seconds:int=Field(default=3600,ge=1,le=86400,strict=True)
class Proposal(Strict):
    action_id:UUID
    run_id:UUID
    session_id:UUID
    parent_turn_id:int=Field(gt=0,strict=True)
    capability:Literal['service.diagnose','service.logs','service.evidence','service.restart']
    typed_args:dict
    user_request:str=Field(min_length=1,max_length=32000)
def arguments(capability,value):
    return (LogsArgs if capability in ('service.logs','service.evidence') else ServiceArgs).model_validate(value).model_dump()
def binding(owner,proposal,args,version,expiry):
    risk,data=CAPABILITIES[proposal.capability]
    return dict(owner_id=owner,run_id=str(proposal.run_id),action_id=str(proposal.action_id),
        capability=proposal.capability,version='1.0',typed_args=args,target_refs=[dict(kind='service',id=args['asset_id'])],
        target_version=version,data_class=data,destination_id='platform_client',destination_version='1',
        policy_version=POLICY,expires_at=expiry)
def verify_grant(token,key):
    try:
        if len(key)<32 or not isinstance(token,str) or len(token)>16000:raise ValueError()
        raw,sig=token.split('.');g=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if not hmac.compare_digest(token,sign_grant(g,key)):raise ValueError()
        fields={'aud','iat','exp','owner_id','run_id','action_id','execution_ref','capability','typed_args','target_version','plan_hash','policy_version'}
        if set(g)!=fields or g['aud']!='ma-ops-execute/v1' or g['policy_version']!=POLICY:raise ValueError()
        if type(g['owner_id']) is not int or g['owner_id']<=0 or type(g['iat']) is not int or type(g['exp']) is not int or g['exp']<=time.time() or g['iat']>time.time()+5 or not 0<g['exp']-g['iat']<=60:raise ValueError()
        for k in ('run_id','action_id','execution_ref'):UUID(g[k])
        for k in ('target_version','plan_hash'):
            if not __import__('re').fullmatch('[a-f0-9]{64}',g[k]):raise ValueError()
        if g['capability'] not in CAPABILITIES or arguments(g['capability'],g['typed_args'])!=g['typed_args']:raise ValueError()
        return g
    except (ValueError,KeyError,TypeError,UnicodeError):raise ValueError('invalid_ops_grant') from None
