"""Stage8 action proposals and read-only projections of the stage9 authority."""
import json
from uuid import uuid5,UUID
from fastapi import HTTPException,Request
from pydantic import BaseModel,ConfigDict,Field
from typing import Literal
from .run_store import unpack,TERMINAL
from .run_worker import AuthVault

class AckArgs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    alarm_id:int=Field(gt=0,le=9007199254740991,strict=True)
class RemarkArgs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    device_id:int=Field(gt=0,le=9007199254740991,strict=True)
    remark:str=Field(max_length=1000,strict=True)
    expected_version:int=Field(ge=0,le=9007199254740991,strict=True)
class ReadArgs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    device_id:int=Field(gt=0,le=9007199254740991,strict=True)
class ActionArgs(BaseModel):
    model_config=ConfigDict(extra='forbid')
    capability:Literal['alarm.ack','device.remark','device.remark.read']
    typed_args:AckArgs|RemarkArgs|ReadArgs

SCHEMA=dict(type='function',function=dict(name='business_action',description='提议一个业务动作：alarm.ack确认单条告警；device.remark修改单台设备备注，必须提供expected_version。若用户已提供版本，直接提议device.remark；否则用普通设备台账取得remark_version。不得用device.remark.read替代用户要求的修改。用户明确请求时才提议，R1暂停等待本人批准；不能表示故障已修复。device.remark.read仅用于用户明确要求读取备注正文，先独立审查，原文仅站内展示不给模型。此工具不能批准、批量操作、控制设备或执行运维。',parameters=ActionArgs.model_json_schema()))

class ActionTools:
    def __init__(self,base,runtime,read_tools=None):
        self.base,self.runtime,self.read_tools=base,runtime,read_tools
        self.schemas=[*base.schemas,SCHEMA];self.call_id=None
        if read_tools is not None:
            self.schemas.append(dict(type='function',function=dict(name='get_device',description='读取指定设备台账的备注版本，仅返回device_id和remark_version，不读取备注正文。修改备注而未提供版本时先调用，再business_action提议device.remark等待本人批准。',parameters=ReadArgs.model_json_schema())))
    def __getattr__(self,key):return getattr(self.base,key)
    async def execute(self,name,args,jwt,result):
        if name=='get_device' and self.read_tools is not None:
            parsed=ReadArgs.model_validate_json(args)
            # Reuse the registered owner-authenticated inventory read, with a
            # smaller projection. No new URL, credential, write or remark read.
            scratch=dict(tool_calls=[],sources=[],warnings=[])
            value=await self.read_tools.execute(name,args,jwt,scratch)
            version=value.get('remark_version')
            if value.get('id')!=parsed.device_id or type(version) is not int or version<0:
                safe=dict(error='device_version_unavailable')
            else:safe=dict(device_id=parsed.device_id,remark_version=version)
            result['tool_calls'].append(dict(name=name,arguments=parsed.model_dump(),status='error' if 'error' in safe else 'ok',result=safe))
            result['sources'].extend(scratch['sources'])
            return safe
        if name!='business_action':return await self.base.execute(name,args,jwt,result)
        parsed=ActionArgs.model_validate_json(args);row=self.runtime.row
        aid=uuid5(row['id'],'action:'+str(self.call_id))
        body=dict(action_id=str(aid),run_id=str(row['id']),session_id=str(row['session_id']),parent_turn_id=row['turn_id'],capability=parsed.capability,typed_args=parsed.typed_args.model_dump(),user_request=row['request']['message'])
        value=await self.runtime.ops.request('POST','/internal/ma-action/v1/actions',body,body)
        if value.get('run_id')!=str(row['id']) or value.get('action_id')!=str(aid):raise HTTPException(409,'action_binding_changed')
        await self.runtime.store.project_authority(row['id'],row['user_id'],[value])
        result['_run_action_pause']=True
        safe=dict(action_id=str(aid),capability=parsed.capability,status=value['status'],message='动作已记录，批准与结果以原轮次动作卡为准；本工具没有批准权限')
        result.setdefault('business_actions',[]).append(safe)
        result['tool_calls'].append(dict(name=name,arguments={'capability':parsed.capability},status='ok' if value['status']!='blocked' else 'error',result=safe))
        return safe

class DecisionBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    decision:Literal['approve','reject']
    approval_id:UUID
    plan_hash:str=Field(pattern='^[a-f0-9]{64}$')

def routes(app):
    @app.post('/internal/agent/runs/{rid}/actions/{aid}/decision')
    async def decide(rid:UUID,aid:UUID,body:DecisionBody,user_id:int,request:Request):
        jwt=request.headers.get('Authorization','').removeprefix('Bearer ')
        if not jwt:raise HTTPException(401,'missing_user_token')
        record=await app.state.runs.pool.fetchrow('SELECT * FROM assistant_runs WHERE id=$1 AND user_id=$2',rid,user_id)
        if not record:raise HTTPException(404,'run_not_found')
        row=unpack(record)
        action=next((x for x in row['actions'] if x['action_id']==str(aid)),None)
        if not action:raise HTTPException(404,'action_not_found')
        prefix='/internal/ma-schedule/v1/actions/' if str(action.get('capability','')).startswith('schedule.') else '/internal/ma-ops/v1/actions/' if str(action.get('capability','')).startswith('service.') else '/internal/ma-action/v1/actions/'
        ops=app.state.run_worker.bound_ops(row,dict(jwt=jwt,actor=row['auth_actor']))
        try:
            value=await ops.request('POST',prefix+str(aid)+'/decision',dict(run_id=str(rid),**body.model_dump(mode='json')))
        except HTTPException as e:
            # Conflict/expiry still synchronizes the authoritative invalid state.
            try:
                value=await ops.request('GET',prefix+str(aid))
                await app.state.runs.project_authority(rid,user_id,[value])
            except Exception:pass
            if e.status_code==503:
                from fastapi.responses import JSONResponse
                return JSONResponse(status_code=202,content={**await app.state.runs.snapshot(rid,user_id),'authority_sync_status':'pending_reconciliation'})
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=e.status_code,content={'detail':e.detail,'error':{'code':e.detail},'run_id':str(rid),'action_id':str(aid)})
        await app.state.runs.project_authority(rid,user_id,[value])
        if row['status'] not in TERMINAL:
            sealed=AuthVault(app.state.config.service_token).seal(user_id,dict(jwt=jwt,actor=row['auth_actor']))
            await app.state.runs.pool.execute("UPDATE assistant_runs SET auth_sealed=$2,deadline=now()+interval '120 seconds' WHERE id=$1",rid,sealed)
        return await app.state.runs.snapshot(rid,user_id)
