import copy
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import pytest
from rag_agent.energy import finish_energy, requested_window

spec=importlib.util.spec_from_file_location('v4_energy_fixture',Path(__file__).parents[1]/'scripts/accept-v4-model.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)


@pytest.mark.asyncio
async def test_seven_day_total_is_checked_not_model_prose():
    ops=fixture.FixtureOps(None,{'id':'Q03'})
    task=await ops.dispatch('第二台设备近7天发电量')
    result={'tool_calls':task['tool_calls'],'_energy_now':'2026-10-23T16:00:00Z','answer_markdown':'模型说发电量99999'}
    assert finish_energy('第二台设备近7天汇总发电量',result)
    assert result['objective_quality']['complete'] and '280' in result['answer_markdown']
    assert '99999' not in result['answer_markdown']
    assert result['objective_quality']['start_date']=='2026-10-17'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['wrong_device','wrong_ordinal','wrong_window','missing_day','missing_value','wrong_sum','nan','incomplete','wrong_scope'])
async def test_energy_target_rejects_mismatch_or_missing_evidence(change):
    task=await fixture.FixtureOps(None,{'id':'Q03'}).dispatch('x')
    calls=copy.deepcopy(task['tool_calls']);data=calls[1]['result']
    if change=='wrong_device': data['device_id']=11
    if change=='wrong_ordinal': calls[0]['result']['list'].reverse()
    if change=='wrong_window': calls[1]['arguments']['start_date']='2026-10-16'
    if change=='missing_day': data['daily'].pop()
    if change=='missing_value': data['daily'][0]['kwh']=None
    if change=='wrong_sum': data['total_kwh']=9999
    if change=='nan': data['total_kwh']=float('nan')
    if change=='incomplete': data['complete']=False;data['missing']=[{'date':'2026-10-17'}]
    if change=='wrong_scope': calls[1]['arguments']['scope']='business'
    result={'tool_calls':calls,'_energy_now':'2026-10-23T16:00:00Z'}
    assert finish_energy('第二台设备近7天发电量',result)
    assert result['objective_quality']['complete'] is False
    assert '目标尚未完成' in result['answer_markdown']


def test_beijing_window_changes_at_midnight_not_utc_midnight():
    before=requested_window('近7天发电量',datetime(2026,10,23,15,59,tzinfo=timezone.utc))
    after=requested_window('近7天发电量',datetime(2026,10,23,16,0,tzinfo=timezone.utc))
    assert [d.isoformat() for d in before]==['2026-10-16','2026-10-23']
    assert [d.isoformat() for d in after]==['2026-10-17','2026-10-24']


def test_no_telemetry_substitution_and_no_historical_energy_replay():
    result={'tool_calls':[{'name':'get_telemetry','status':'ok','result':{'power':99}}]}
    assert finish_energy('第二台设备近7天发电量',result)
    assert not result['objective_quality']['complete']
    assert '24小时遥测不能替代' in result['answer_markdown']


def test_explanation_is_not_replaced_by_an_energy_query():
    from rag_agent import v4
    result={'tool_calls':[],'answer_markdown':'按日聚合值相加。'}
    assert v4.intent('解释近7天发电量怎么计算')=='explanation'
    v4.finish('解释近7天发电量怎么计算',result,[])
    assert result['answer_markdown']=='按日聚合值相加。'


def test_missing_weather_source_does_not_override_technical_answer():
    from rag_agent import v4
    result={'tool_calls':[],'answer_markdown':'MPPT动态跟踪工作点。\n告诉我城市，我马上帮你查天气。','warnings':[]}
    v4.finish('解释MPPT，今天会下雨吗',result,[])
    assert 'MPPT动态跟踪工作点' in result['answer_markdown']
    assert '马上帮你查' not in result['answer_markdown'] and '无法确认' in result['answer_markdown']


@pytest.mark.asyncio
async def test_successful_wrong_device_cannot_complete_ordinal_objective():
    from rag_agent import v4
    task=await fixture.FixtureOps(None,{'id':'Q02'}).dispatch('读取第二台设备')
    result={'tool_calls':task['tool_calls'],'answer_markdown':'已成功','sources':[],'warnings':[]}
    v4.finish('第一台设备状态如何',result,[])
    assert result['objective_quality']['complete'] is False
    assert result['objective_quality']['expected_device_id']==11
    assert '目标尚未完成' in result['answer_markdown']
