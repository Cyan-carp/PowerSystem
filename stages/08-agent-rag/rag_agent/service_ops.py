"""S07 proposals and registry discovery. Sensitive results never return to a model."""
import json
from uuid import uuid5
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field
from fastapi import HTTPException

class OpsArgs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    capability:Literal['service.diagnose','service.logs','service.evidence','service.restart']
    asset_id:str=Field(pattern='^[a-z][a-z0-9_-]{0,79}$',strict=True)
    limit:int|None=Field(default=None,ge=1,le=100,strict=True)
    since_seconds:int|None=Field(default=None,ge=1,le=86400,strict=True)
SCHEMA=dict(type='function',function=dict(name='ops_action',description='提议固定单个登记隔离服务动作。先ops_assets取得准确asset_id，不猜目标。service.diagnose仅展示诊断；service.logs仅展示过滤日志，不生成文件；service.evidence在一次动作中同时收集诊断和过滤日志并生成JSON证据包，用户要证据包必须选此能力；service.restart仅用户明确要求且管理员一次批准后重启。本工具不能批准，不接受Shell/SQL/URL/设备控制/批量/定时。日志和证据原文只站内展示，不给模型。limit/since_seconds只适用日志和证据。',parameters=OpsArgs.model_json_schema()))
ASSETS=dict(type='function',function=dict(name='ops_assets',description='读取本阶段可用的登记隔离服务及固定能力；不探测新目标，不授予操作权限。',parameters=dict(type='object',properties={},additionalProperties=False)))

class ServiceTools:
    def __init__(self,base,runtime):self.base,self.runtime=base,runtime;self.schemas=[*base.schemas,ASSETS,SCHEMA];self.call_id=None
    def __getattr__(self,key):return getattr(self.base,key)
    async def execute(self,name,args,jwt,result):
        if name=='ops_assets':
            if json.loads(args)!={}:raise ValueError('invalid_ops_asset_args')
            return await self.runtime.ops.request('GET','/internal/ma-ops/v1/assets')
        if name!='ops_action':return await self.base.execute(name,args,jwt,result)
        value=OpsArgs.model_validate_json(args);row=self.runtime.row
        typed=value.model_dump(exclude={'capability'},exclude_none=True)
        if value.capability not in ('service.logs','service.evidence') and set(typed)!={'asset_id'}:raise ValueError('invalid_ops_action_args')
        aid=uuid5(row['id'],'ops:'+str(self.call_id))
        body=dict(action_id=str(aid),run_id=str(row['id']),session_id=str(row['session_id']),parent_turn_id=row['turn_id'],capability=value.capability,typed_args=typed,user_request=row['request']['message'])
        try:action=await self.runtime.ops.request('POST','/internal/ma-ops/v1/actions',body,body)
        except HTTPException as exc:
            if exc.status_code not in (400,403,409):raise
            reason='该动作需要管理员本人批准，本轮没有执行服务操作。' if exc.detail=='admin_required' else '动作未通过权限、参数或登记目标核对，本轮没有执行服务操作。'
            safe=dict(error=str(exc.detail),message=reason);result['ops_failure']=safe
            result['tool_calls'].append(dict(name=name,arguments=dict(capability=value.capability),status='error',error=str(exc.detail)))
            return safe
        if action.get('run_id')!=str(row['id']) or action.get('action_id')!=str(aid):raise ValueError('action_binding_changed')
        await self.runtime.store.project_authority(row['id'],row['user_id'],[action])
        result['_run_action_pause']=True
        safe=dict(action_id=str(aid),capability=value.capability,status=action['status'],message='动作事实以原轮次动作卡为准；本工具无批准权限，不向模型回传日志或证据内容')
        result.setdefault('ops_actions',[]).append(safe);result['tool_calls'].append(dict(name=name,arguments=dict(capability=value.capability),status='ok' if action['status']!='blocked' else 'error',result=safe))
        return safe
