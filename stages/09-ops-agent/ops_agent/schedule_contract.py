"""S08 configuration action domain. No operations or caller-selected destinations."""
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from .business_contract import Strict,digest,sign_grant,canonical
import base64,hmac,json,time

POLICY='v4-s08-local/v1';PROTOCOL='ma-schedule/v1'
CAPABILITIES={k:('R1','internal') for k in ('schedule.create','schedule.update','schedule.resume')}
class ScheduleConfig(Strict):
    task_type:Literal['daily_report','inspection']
    scope:Literal['all','business','baseline']='business'
    station_code:str|None=Field(default=None,pattern='^[A-Za-z0-9_-]{1,64}$')
    device_id:int|None=Field(default=None,gt=0,le=9007199254740991,strict=True)
    timezone:str=Field(default='Asia/Shanghai',min_length=1,max_length=64)
    mode:Literal['daily','interval']='daily'
    daily_time:str|None='09:00'
    interval_minutes:int|None=Field(default=None,ge=15,le=10080,strict=True)
    grant_days:int=Field(default=30,ge=1,le=90,strict=True)
    destination_id:Literal['platform_client']='platform_client'
    destination_version:Literal['1']='1'
    @model_validator(mode='after')
    def clock(self):
        import re
        from zoneinfo import ZoneInfo
        try:ZoneInfo(self.timezone)
        except (KeyError,ValueError):raise ValueError('invalid_timezone') from None
        if self.mode=='daily' and (self.interval_minutes is not None or not isinstance(self.daily_time,str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',self.daily_time)):raise ValueError('invalid_daily_time')
        if self.mode=='interval' and (self.daily_time is not None or self.interval_minutes is None):raise ValueError('invalid_interval')
        return self
class ConfigArgs(Strict):
    schedule_id:UUID
    expected_version:int=Field(ge=0,le=9007199254740991,strict=True)
    config:ScheduleConfig
class Proposal(Strict):
    action_id:UUID;run_id:UUID;session_id:UUID
    parent_turn_id:int=Field(gt=0,strict=True)
    capability:Literal['schedule.create','schedule.update','schedule.resume']
    typed_args:dict
    user_request:str=Field(min_length=1,max_length=32000)
def arguments(cap,value):
    args=ConfigArgs.model_validate(value).model_dump(mode='json')
    if cap=='schedule.create' and args['expected_version']!=0:raise ValueError('create_version')
    if cap!='schedule.create' and args['expected_version']<1:raise ValueError('update_version')
    return args
def binding(owner,proposal,args,version,expiry):
    return dict(owner_id=owner,run_id=str(proposal.run_id),action_id=str(proposal.action_id),capability=proposal.capability,version='1.0',typed_args=args,target_refs=[dict(kind='schedule',id=args['schedule_id'])],target_version=version,data_class='internal',destination_id='platform_client',destination_version='1',policy_version=POLICY,expires_at=expiry)
def verify_grant(token,key,expected_policy=POLICY):
    try:
        if len(key)<32 or not isinstance(token,str) or len(token)>16000:raise ValueError()
        raw,_=token.split('.');g=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if not hmac.compare_digest(token,sign_grant(g,key)):raise ValueError()
        if expected_policy not in (POLICY,'v4-s08-production/v1'):raise ValueError()
        if set(g)!={'aud','iat','exp','owner_id','run_id','action_id','execution_ref','capability','typed_args','target_version','plan_hash','policy_version'} or g['aud']!='ma-schedule-execute/v1' or g['policy_version']!=expected_policy:raise ValueError()
        if type(g['owner_id']) is not int or g['owner_id']<=0 or type(g['iat']) is not int or type(g['exp']) is not int or not time.time()<g['exp']<=g['iat']+60 or g['iat']>time.time()+5:raise ValueError()
        for k in ('run_id','action_id','execution_ref'):UUID(g[k])
        if g['capability'] not in CAPABILITIES or arguments(g['capability'],g['typed_args'])!=g['typed_args']:raise ValueError()
        return g
    except (ValueError,KeyError,TypeError):raise ValueError('invalid_schedule_action_grant') from None
