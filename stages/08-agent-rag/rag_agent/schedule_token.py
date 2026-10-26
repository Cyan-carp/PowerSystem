"""Verify only short configuration dispatches; never accepts persistent read grants."""
import base64,json,hmac,hashlib,time
from uuid import UUID
from .schedules import ScheduleConfig
from .ma_security import canonical

def verify_action(token,key,expected_policy='v4-s08-local/v1'):
    try:
        if len(key)<32 or not isinstance(token,str) or len(token)>16000:raise ValueError()
        raw,sig=token.split('.')
        expected=base64.urlsafe_b64encode(hmac.new(key.encode(),raw.encode(),hashlib.sha256).digest()).decode().rstrip('=')
        if not hmac.compare_digest(sig,expected):raise ValueError()
        g=json.loads(base64.urlsafe_b64decode(raw+'='*(-len(raw)%4)))
        if expected_policy not in ('v4-s08-local/v1','v4-s08-production/v1'):raise ValueError()
        if set(g)!={'aud','iat','exp','owner_id','run_id','action_id','execution_ref','capability','typed_args','target_version','plan_hash','policy_version'} or g['aud']!='ma-schedule-execute/v1' or g['policy_version']!=expected_policy or g['capability'] not in ('schedule.create','schedule.update','schedule.resume'):raise ValueError()
        if type(g['iat']) is not int or type(g['exp']) is not int or not time.time()<g['exp']<=g['iat']+60 or g['iat']>time.time()+5 or type(g['owner_id']) is not int or g['owner_id']<1:raise ValueError()
        for k in ('run_id','action_id','execution_ref'):UUID(g[k])
        args=g['typed_args']
        if set(args)!={'schedule_id','expected_version','config'} or type(args['expected_version']) is not int or args['expected_version']<0:raise ValueError()
        UUID(args['schedule_id'])
        if ScheduleConfig.model_validate(args['config']).model_dump()!=args['config']:raise ValueError()
        return g
    except (ValueError,KeyError,TypeError):raise ValueError('invalid_schedule_action_grant') from None
