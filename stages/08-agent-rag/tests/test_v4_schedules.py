from datetime import datetime,timezone
from uuid import uuid4
import pytest
from rag_agent.schedules import ScheduleConfig,Preview
from rag_agent.schedule_time import next_slot,missed_count
from rag_agent.rule_preview import evaluate
from rag_agent.schedule_token import verify_action
from ops_agent.schedule_contract import sign_grant,arguments

def dt(s):return datetime.fromisoformat(s)
def test_clock_defaults_minimum_and_expiry():
    cfg=ScheduleConfig(task_type='daily_report').model_dump()
    assert cfg['timezone']=='Asia/Shanghai' and cfg['grant_days']==30
    assert next_slot(cfg,dt('2026-10-23T23:00:00+00:00'))==dt('2026-10-24T01:00:00+00:00')
    for value in (dict(interval_minutes=14,mode='interval',daily_time=None),dict(grant_days=91),dict(destination_id='external'),dict(timezone='invalid/zone')):
        with pytest.raises((ValueError,KeyError)):ScheduleConfig(task_type='inspection',**value)
def test_interval_missed_and_daily_dst():
    cfg=ScheduleConfig(task_type='inspection',mode='interval',daily_time=None,interval_minutes=15).model_dump()
    assert next_slot(cfg,dt('2026-10-24T00:00:01+00:00'))==dt('2026-10-24T00:15:00+00:00')
    assert missed_count(cfg,dt('2026-10-24T00:00:00+00:00'),dt('2026-10-24T01:00:00+00:00'))==5
    cfg.update(mode='daily',daily_time='02:30',interval_minutes=None,timezone='America/Los_Angeles')
    assert next_slot(cfg,dt('2026-03-08T09:59:00+00:00'))==dt('2026-03-09T09:30:00+00:00')
    cfg['daily_time']='01:30'
    assert next_slot(cfg,dt('2026-11-01T08:40:00+00:00'))==dt('2026-11-02T09:30:00+00:00')
def test_snapshot_replay_and_missing_without_mutation():
    args=Preview(device_id=801,metric='temperature',start='2026-10-23T00:00:00+08:00',end='2026-10-23T00:00:30+08:00',operator='gt',threshold=70,baseline_threshold=75).model_dump()
    start=int(dt(args['start']).timestamp()*1000)
    snap=dict(device_id=801,metric='temperature',points=[[start,60],[start+5000,72],[start+10000,80],[start+15000,None],[start+20000,74],[start+25000,78]])
    result=evaluate(args,snap)
    assert result['coverage']['missing_samples']==1 and result['metrics']['candidate_hits']==4 and result['metrics']['baseline_hits']==2
    assert not result['coverage']['complete'] and snap['points'][3][1] is None
def test_short_dispatch_domain_and_long_grant_cannot_cross():
    import time
    at=int(time.time());key='x'*40
    cfg=ScheduleConfig(task_type='inspection').model_dump()
    args=dict(schedule_id=str(uuid4()),expected_version=0,config=cfg)
    g=dict(aud='ma-schedule-execute/v1',iat=at,exp=at+60,owner_id=80001,run_id=str(uuid4()),action_id=str(uuid4()),execution_ref=str(uuid4()),capability='schedule.create',typed_args=args,target_version='a'*64,plan_hash='b'*64,policy_version='v4-s08-local/v1')
    assert verify_action(sign_grant(g,key),key)==g
    for change in (dict(aud='ma-business-execute/v1'),dict(exp=at+86400),dict(capability='service.restart'),dict(policy_version='v4-s07-local/v1')):
        with pytest.raises(ValueError):verify_action(sign_grant({**g,**change},key),key)
    with pytest.raises(ValueError):arguments('schedule.create',{**args,'config':{**cfg,'task_type':'service.restart'}})
