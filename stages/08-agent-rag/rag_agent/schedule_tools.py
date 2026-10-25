"""Optional interactive tools. Scheduled runs never enter the model tool graph."""
import json
from typing import Literal
from uuid import UUID,uuid5
from pydantic import Field
from .schedules import Strict,ScheduleConfig,Preview

class ConfigTool(Strict):
    capability:Literal['schedule.create','schedule.update','schedule.resume']
    config:ScheduleConfig
    schedule_id:UUID|None=None
    expected_version:int=Field(default=0,ge=0,strict=True)

SCHEMAS=[dict(type='function',function=dict(name='schedule_task',description='提议本人定期日报或巡检配置。daily_report上一完整北京时间自然日电量，inspection时槽前24h告警调查。仅本站本人交付；创建/修改/恢复须独立审查和本人5分钟一次批准，再形成默认30天最多90天的有限只读授权。默认Asia/Shanghai，固定间隔至少15分钟且daily_time=null。不能批准动作、运维、外发或自动续期。修改/恢复必须提供已有schedule_id与expected_version，不猜ID。',parameters=ConfigTool.model_json_schema())),dict(type='function',function=dict(name='rule_preview',description='本人明确提供设备、带时区历史窗口（最多24h）、指标、比较符、基准阈值和候选阈值时，只读历史预演。比较采样命中而非真实收益，不写线上规则；缺少参数应询问。',parameters=Preview.model_json_schema()))]
class ScheduleTools:
    def __init__(self,base,runtime,service):self.base,self.runtime,self.service=base,runtime,service;self.schemas=[*base.schemas,*SCHEMAS];self.call_id=None
    def __getattr__(self,key):return getattr(self.base,key)
    async def execute(self,name,args,jwt,result):
        if name not in ('schedule_task','rule_preview'):return await self.base.execute(name,args,jwt,result)
        if name=='rule_preview':
            await self.service.preview(self.runtime,dict(jwt=jwt),result,Preview.model_validate_json(args))
            return dict(status='completed',message=result['answer_markdown'],artifacts=result['artifacts'])
        parsed=ConfigTool.model_validate_json(args);row=self.runtime.row
        sid=parsed.schedule_id or uuid5(row['id'],'schedule:'+str(self.call_id))
        if parsed.capability!='schedule.create' and (not parsed.schedule_id or parsed.expected_version<1):raise ValueError('existing_schedule_required')
        if parsed.capability=='schedule.create' and parsed.expected_version!=0:raise ValueError('create_version')
        typed=dict(schedule_id=str(sid),expected_version=parsed.expected_version,config=parsed.config.model_dump())
        body=dict(action_id=str(uuid5(row['id'],'schedule:'+str(self.call_id))),run_id=str(row['id']),session_id=str(row['session_id']),parent_turn_id=row['turn_id'],capability=parsed.capability,typed_args=typed,user_request=row['request']['message'])
        value=await self.runtime.ops.request('POST','/internal/ma-schedule/v1/actions',body,body)
        await self.runtime.store.project_authority(row['id'],row['user_id'],[value]);result['_run_action_pause']=True
        safe=dict(action_id=value['action_id'],capability=parsed.capability,status=value['status'],message='配置以本轮审批卡和权威事实为准；尚未批准不生效')
        result.setdefault('tool_calls',[]).append(dict(name=name,arguments={'capability':parsed.capability},status='ok',result=safe))
        return safe
