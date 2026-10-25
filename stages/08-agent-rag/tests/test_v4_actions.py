import pytest
from rag_agent.actions import ActionArgs
from rag_agent import v4
from pydantic import ValidationError

def test_action_schema_exposes_real_typed_parameters_and_rejects_invented_alias():
    schema=ActionArgs.model_json_schema();assert 'remark' in schema['$defs']['RemarkArgs']['properties']
    with pytest.raises(ValidationError):ActionArgs(capability='device.remark',typed_args={'device_id':1,'expected_version':0,'new_remark':'text'})

def test_investigation_cannot_fulfil_explicit_acknowledgement_request():
    result={'capability_catalog':{'registered_tools':['business_report','business_action']},'reports':[{'source':'/alarms'}],'tool_calls':[]}
    assert v4.finish('请确认告警ID 60618',result,[])
    assert result['status']=='degraded' and result['objective_quality']['complete'] is False
    assert '未执行业务修改' in result['answer_markdown']

def test_explanation_and_explicit_no_action_do_not_force_a_write_proposal():
    for q in ('解释确认告警是什么意思','不要确认告警60618，只解释它','告警确认如何操作'):
        assert not v4.action_request(q),q
    assert v4.explicit_action_target('请确认告警ID 60618')
    assert v4.explicit_action_target('请将设备ID 619的备注改为合成，当前版本为0')


def test_remark_proposal_without_version_can_read_only_inventory_version():
    import asyncio,json
    from types import SimpleNamespace
    from rag_agent.actions import ActionTools
    class Inventory:
        async def execute(self,name,args,jwt,result):
            assert name=='get_device' and jwt=='owner-jwt'
            return dict(id=1,remark_version=7,remark='must never cross projection',extra='discard')
    tools=ActionTools(SimpleNamespace(schemas=[]),None,Inventory())
    result=dict(tool_calls=[],sources=[])
    value=asyncio.run(tools.execute('get_device','{"device_id":1}','owner-jwt',result))
    assert value==dict(device_id=1,remark_version=7)
    assert 'must never' not in json.dumps(result)
    assert v4.remark_action_target('请把设备1的备注改为“本人验收拒绝样本”，从台账读取当前备注版本，不读取备注正文。')
    assert not v4.remark_action_target('不要修改设备1的备注')


def test_remark_version_never_guesses_missing_or_mismatched_inventory():
    import asyncio
    from types import SimpleNamespace
    from rag_agent.actions import ActionTools
    for bad in ({'id':2,'remark_version':7},{'id':1},{'id':1,'remark_version':True}):
        class Inventory:
            async def execute(self,*args):return bad
        tools=ActionTools(SimpleNamespace(schemas=[]),None,Inventory())
        value=asyncio.run(tools.execute('get_device','{"device_id":1}','jwt',dict(tool_calls=[],sources=[])))
        assert value==dict(error='device_version_unavailable')
