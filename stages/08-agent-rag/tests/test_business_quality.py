import copy
import json
from datetime import datetime, timezone

import pytest

from rag_agent.business_quality import (applicability, build_quality, business_request,
    finalize_diagnosis, guard_answer, quality_summary, render_quality, valid_quality)

AT = '2026-10-19T05:00:00+00:00'
TS = int(datetime.fromisoformat(AT).timestamp() * 1000)


def call(name, data, args=None, status='ok'):
    return {'name': name, 'arguments': args or {}, 'status': status, 'collected_at': AT, 'result': data}


def calls(operator='>', value=400.93, threshold=240, **overrides):
    rule = {'id': 8, 'device_id': 2, 'metric': 'voltage', 'unit': 'V', 'operator': operator, 'threshold': threshold, 'enabled': True, 'version': 'v3'}
    rule.update(overrides)
    return [call('get_device', {'id': 2, 'device_code': 'INV-TEST', 'vendor': 'Huawei'}, {'device_id': 2}),
        call('get_telemetry', {'device_id': 2, 'ts_ms': TS, 'received_at': AT, 'voltage': value, 'status': 1}, {'device_id': 2}),
        call('get_alarm_rules', {'list': [rule], 'total': 1, 'page': 1}, {'device_id': 2})]


@pytest.mark.parametrize('operator,value,threshold,hit', [('>',400.93,240,True), ('<',400.93,240,False), ('>',240,240,False), ('>=',240,240,True), ('<',240,240,False), ('<=',240,240,True), ('<',239.99,240,True)])
def test_threshold_is_computed_not_model_prose(operator,value,threshold,hit):
    q=build_quality(calls(operator,value,threshold))
    fact=next(f for f in q['facts'] if f['kind']=='threshold')
    assert fact['data']['triggered']==hit and len(fact['call_refs'])==2 and fact['device_id']==2
    answer,state=render_quality(q, {'summary':'400.93V低于240V，设备完全健康'})
    assert state=='degraded' and '低于240V' not in answer and '设备完全健康' not in answer
    assert '不能据单条规则推断整体健康' in answer


@pytest.mark.parametrize('changes', [{'unit':''},{'unit':'kV'},{'enabled':False},{'version':''},{'operator':'='}])
def test_missing_or_inapplicable_rule_fails_closed(changes):
    q=build_quality(calls(**changes))
    assert not any(f['kind']=='threshold' for f in q['facts']) and q['missing']


def test_old_or_wrong_device_sample_is_not_current_threshold_evidence():
    rows=calls();rows[1]['result']['ts_ms']=TS-3600000
    assert not any(f['kind']=='threshold' for f in build_quality(rows)['facts'])
    rows=calls();rows[1]['result']['device_id']=99
    assert not any(f['kind']=='telemetry' for f in build_quality(rows)['facts'])


@pytest.mark.parametrize('status,reason,expected', [('unhandled',None,'未确认且未恢复'),('acked',None,'已确认但未恢复'),('recovered','telemetry_recovered','遥测恢复'),('recovered','rule_disabled','管理关闭'),('recovered','rule_deleted','管理关闭'),('recovered','device_deleted','管理关闭'),('recovered','unknown','原因未知')])
def test_alarm_lifecycle_is_program_rendered(status,reason,expected):
    alarm={'id':7,'device_id':2,'status':status,'close_reason':reason,'triggered_at':AT,'acked_at':AT if status=='acked' else None,'recovered_at':AT if status=='recovered' else None,'metric':'voltage','value':400.93,'threshold':240,'rule_operator':'>','rule_version':'v1'}
    q=build_quality([call('list_alarms',{'list':[alarm],'total':2,'page':1},{'device_id':2})])
    answer,_=render_quality(q,None)
    assert expected in answer and '部分分页' in answer
    if status=='acked': assert '已恢复' not in answer
    if status=='recovered': assert '不计入当前活动告警' in answer


def test_historical_threshold_uses_snapshot_not_current_rule():
    rows=calls('>',400.93,500)
    alarm={'id':9,'device_id':2,'status':'recovered','recovered_at':AT,'triggered_at':AT,'metric':'voltage','value':400.93,'threshold':240,'rule_operator':'>','rule_version':'v1'}
    rows.append(call('get_alarm_detail',alarm,{'alarm_id':9}))
    q=build_quality(rows)
    old=next(f for f in q['facts'] if f['kind']=='historical_threshold')
    current=next(f for f in q['facts'] if f['kind']=='threshold')
    assert old['data']['threshold']==240 and old['data']['triggered'] is True and old['data']['rule_version']=='v1'
    assert current['data']['threshold']==500 and current['data']['triggered'] is False
    del alarm['rule_operator']
    q=build_quality(rows)
    assert not any(f['kind']=='historical_threshold' for f in q['facts']) and any('不使用当前规则反推' in m for m in q['missing'])


def test_selection_hash_partial_failure_and_unbound_reference():
    rows=calls();rows.append(call('get_prediction',{}, {'device_id':2}, 'error'))
    q=build_quality(rows)
    assert valid_quality(q) and q['state']=='partial_failed'
    selected={'fact_refs':[q['facts'][0]['id']], 'explanation_type':'facts_only'}
    assert render_quality(q,selected)[1]=='validated'
    selected['fact_refs']=['Funknown']
    assert render_quality(q,selected)[1]=='degraded'
    q['facts'][0]['text']='tampered'
    assert not valid_quality(q)
    with pytest.raises(ValueError): quality_summary(q)


def test_zero_call_repeat_and_historical_task_do_not_become_fresh():
    previous=[{'response':{'business_quality':build_quality(calls())}}]
    out={'answer_markdown':'刚刚查到设备很健康','tool_calls':[]}
    assert guard_answer('那现在呢？',out,previous)
    assert '未取得平台业务证据' in out['answer_markdown'] and '刚刚查到' not in out['answer_markdown']
    out={'answer_markdown':'当前设备很健康','tool_calls':[], 'task_cards':[{'task_id':'old','historical':True,'business_quality':build_quality(calls())}]}
    guard_answer('解释历史任务',out)
    assert '仅说明当时记录' in out['answer_markdown'] and out['business_quality']['scope']=='historical'
    result={'tool_calls':calls(),'diagnosis':{'summary':'400.93低于240'}}
    finalize_diagnosis(result,'查询设备')
    assert valid_quality(result['business_quality']) and '低于240' not in result['diagnosis']['summary']


@pytest.mark.parametrize('state', ['queued','planning','executing'])
def test_pending_task_never_claims_queried(state):
    out={'answer_markdown':'已查到最新数据','tool_calls':[],'task_cards':[{'status':state}]}
    guard_answer('查设备',out)
    assert '受理成功不代表查询完成' in out['answer_markdown'] and '已查到最新数据' not in out['answer_markdown']


def test_manual_applicability_and_document_versions():
    device={'vendor':'Huawei','model':'M1','region_variant':'CN','manual_version':'v1','grid':'400V'}
    meta={'source_type':'manufacturer_manual','manufacturer':'Huawei','full_model':'M1','region_variant':'CN','document_number':'D1','document_version':'v1','registered':True,'conditions':{'grid':'400V'}}
    assert applicability(device,meta)[0]=='match'
    for field in ('manufacturer','full_model','region_variant','document_version'):
        bad={**meta,field:'other'}
        assert applicability(device,bad)[0]=='mismatch'
    assert applicability({**device,'is_simulated':True},meta)[0]=='simulation_reference'
    assert applicability(device,{'source_type':'project_summary'})[0]=='project_summary'
    assert applicability(device,None)[0]=='insufficient'
    assert applicability({k:v for k,v in device.items() if k!='model'},meta)[0]=='insufficient'
    assert applicability(device,{**meta,'conditions':{'grid':'690V'}})[0]=='mismatch'
    q=build_quality([call('get_device',{'id':2,**device},{'device_id':2}),call('search_knowledge',{'results':[{'id':'K1','version':'collection-v9','applicability':meta}]})])
    fact=next(f for f in q['facts'] if f['kind']=='applicability')
    assert fact['data']['state']=='match' and fact['data']['source_version']=='collection-v9' and fact['call_refs']


def test_ordinary_technical_explanation_remains_free():
    assert not business_request('解释设备告警的原理')
    out={'answer_markdown':'普通技术解释','tool_calls':[]}
    assert not guard_answer('解释设备告警的原理',out)
    assert out['answer_markdown']=='普通技术解释'
    assert business_request('查询 A-02 的台账和遥测')
    out={'tool_calls':[], 'error':{'code':'model_http_503'}}
    finalize_diagnosis(out,'查询 A-02 的台账和遥测')
    assert out['business_quality']['state']=='failed'


def test_legacy_history_explanation_uses_recorded_calls_not_old_prose():
    legacy={'task_id':'old','historical':True,'tool_calls':calls(),'diagnosis':{'summary':'错误的旧正文'}}
    original=copy.deepcopy(legacy)
    out={'answer_markdown':'当前设备完全健康','tool_calls':[],'task_cards':[legacy]}
    assert guard_answer('解释历史任务',out)
    assert '仅说明当时记录' in out['answer_markdown'] and '错误的旧正文' not in out['answer_markdown']
    assert legacy==original


@pytest.mark.parametrize('partial', [False, True])
def test_current_delegated_success_reports_evidence_in_this_round(partial):
    rows=calls()
    if partial: rows.append(call('get_prediction',{}, {'device_id':2}, 'error'))
    out={'answer_markdown':'刚刚查到完全健康','tool_calls':[], 'task_cards':[{'task_id':'new','status':'succeeded','historical':False,'business_quality':build_quality(rows)}]}
    assert guard_answer('核验 A-02',out)
    q=out['business_quality']
    assert valid_quality(q) and q['state']==('partial_failed' if partial else 'queried')
    assert '未取得平台业务证据' not in out['answer_markdown'] and '完全健康' not in out['answer_markdown']


@pytest.mark.asyncio
async def test_supervisor_replaces_conflicting_prose_and_persists_only_program_answer():
    from rag_agent.engine import Engine
    from rag_agent.config import Config
    class Provider:
        def __init__(self, replies): self.replies = list(replies)
        async def complete(self, messages, schemas): return self.replies.pop(0)
    class Tools:
        schemas=[]
        async def execute(self,name,args,jwt,result):
            result['tool_calls'].extend(calls())
            return {'ok':True}
    provider=Provider([{'role':'assistant','content':None,'tool_calls':[{'id':'x','function':{'name':'dispatch_ops_task','arguments':'{}'}}]}, {'role':'assistant','content':'400.93V低于240V，设备完全健康，刚刚查询时间00:00。'}])
    result={'status':'degraded','answer_markdown':'','tool_calls':[],'sources':[],'warnings':[]}
    config=Config('fixture','https://example.invalid','fixture','s'*32,'postgresql://private','redis://private')
    messages=await Engine(config,provider,Tools()).run('第二台设备现在怎么样',[],'jwt',result)
    assert '低于240V' not in result['answer_markdown'] and '刚刚查询时间00:00' not in result['answer_markdown']
    assert valid_quality(result['business_quality']) and messages[-1]['content']==result['answer_markdown']
    assert all('设备完全健康' not in m.get('content','') for m in messages)


def alarm_list_calls(active=0, total=50):
    rows = [{'id':n, 'device_id':2, 'status':'recovered', 'close_reason':'unknown',
             'triggered_at':AT, 'acked_at':None, 'recovered_at':AT,
             'metric':'voltage', 'value':400.93, 'threshold':240,
             'rule_operator':'>', 'rule_version':'v1'} for n in range(1, 51)]
    rows += [{'id':100+n, 'device_id':2, 'status':'acked', 'acked_at':AT,
              'triggered_at':AT, 'recovered_at':None} for n in range(active)]
    return [call('list_alarms', {'list':rows, 'total':total, 'page':1}, {'device_id':2})]


def test_default_alarm_answer_summarizes_history_without_mutating_signed_facts():
    q = build_quality(alarm_list_calls())
    original = copy.deepcopy(q)
    answer, _ = render_quality(q)
    assert '原因未知 50 条' in answer and '不计入当前活动告警' in answer
    assert '不能证明故障消除' in answer
    assert '告警 1：' not in answer and '触发时快照' not in answer
    assert len(answer) < 800 and q == original and valid_quality(q)
    assert len([f for f in q['facts'] if f['kind']=='alarm']) == 50


def test_active_alarms_after_history_are_visible_and_partial_scope_is_preserved():
    q = build_quality(alarm_list_calls(active=7, total=200))
    selection = {'fact_refs':[f['id'] for f in q['facts'] if f['kind']=='historical_threshold'], 'explanation_type':'facts_only'}
    answer, state = render_quality(q, selection)
    assert state == 'validated' and '已确认但未恢复 7 条' in answer
    assert sum('告警 '+str(100+n)+'：' in answer for n in range(7)) == 5
    assert '部分分页' in answer and '不代表当前活动告警全量' in answer
    assert '告警 1：' not in answer and '触发时快照' not in answer
    assert 'None' not in answer and '2026-10-19 13:00:00 +08:00' in answer


def test_explicit_history_request_is_bounded_and_detail_request_remains_available():
    q = build_quality(alarm_list_calls())
    answer, _ = render_quality(q, question='查询该设备历史告警')
    assert sum('告警 '+str(n)+'：' in answer for n in range(1,51)) == 5
    assert answer.count('告警触发时快照') == 5 and '最多 5 条明细' in answer
    assert 'None' not in answer
    alarm = alarm_list_calls()[0]['result']['list'][0]
    q = build_quality([call('get_alarm_detail',alarm,{'alarm_id':1})])
    answer, _ = render_quality(q, question='查看告警1详情')
    assert '告警 1：' in answer and '告警触发时快照' in answer and 'None' not in answer


@pytest.mark.parametrize('historical', [False, True])
def test_delegated_alarm_summary_retains_history_scope_and_security_binding(historical):
    q = build_quality(alarm_list_calls())
    bound = quality_summary(q)
    out = {'answer_markdown':'完全健康', 'tool_calls':[], 'task_cards':[
        {'task_id':'task', 'historical':historical, 'status':'succeeded', 'business_quality':q}]}
    guard_answer('查询这台设备告警', out)
    assert '原因未知 50 条' in out['answer_markdown'] and len(out['answer_markdown']) < 1000
    assert '完全健康' not in out['answer_markdown'] and quality_summary(q) == bound
    assert valid_quality(out['business_quality'])
    if historical:
        assert '历史任务当时返回的记录' in out['answer_markdown'] and '仅说明当时记录' in out['answer_markdown']


def test_alarm_summary_cannot_hide_threshold_or_manual_applicability():
    rows = calls()+alarm_list_calls()
    rows.append(call('search_knowledge', {'results':[{'id':'K','version':'v1','applicability':{'source_type':'project_summary'}}]}))
    out = {'tool_calls':rows}
    finalize_diagnosis(out,'查询设备现在的情况')
    assert '400.93 V > 240.0 V' in out['diagnosis']['summary']
    assert '项目摘要，不是厂家手册' in out['diagnosis']['summary']
    assert '原因未知 50 条' in out['diagnosis']['summary'] and valid_quality(out['business_quality'])
