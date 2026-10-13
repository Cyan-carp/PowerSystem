"""Sensitive task reports cannot cross Supervisor model/history boundaries directly."""
import json
import time
from types import SimpleNamespace
from uuid import uuid4
import pytest
from rag_agent.ops import SupervisorTools
from rag_agent.engine import Engine
from rag_agent.ma_security import digest
pytestmark = pytest.mark.asyncio


class Ops:
    def __init__(self,fail=False):
        self.cfg=SimpleNamespace(acceptance_drill_only=True,llm_url='https://hgapi.dieqiyun.top/v1',model='deepseek-v4.1-flash')
        self.fail=fail
        self.task={'task_id':str(uuid4()),'status':'succeeded','infrastructure_evidence':[{'asset_id':'local-drill'}],
            'security_reviews':[{'state':'completed'}],'tool_calls':[{'raw':'PRIVATE_REPORT_PROSE'}], 'diagnosis':{'summary':'PRIVATE_REPORT_PROSE'}}
    async def get(self,tid):return self.task
    async def request(self,*args):
        if self.fail: raise ValueError('security_denied')
        if args[1].endswith('/check'):return {'permitted':True}
        payload={'task_status':'succeeded','evidence':[],'actions':[]}
        return {'payload':payload,'payload_hash':digest(payload),'review_id':str(uuid4())}


@pytest.mark.parametrize('fail',[False,True])
@pytest.mark.parametrize('collected',[False,True])
async def test_sensitive_get_sends_only_reviewed_summary_and_retains_card(fail,collected):
    ops=Ops(fail);tools=SupervisorTools(None,ops,time.monotonic()+30)
    if not collected:ops.task['infrastructure_evidence']=[]
    result={'tool_calls':[],'warnings':[],'task_cards':[]}
    payload=await tools.execute('get_ops_task',json.dumps({'task_id':ops.task['task_id']}),'jwt',result)
    assert 'PRIVATE_REPORT_PROSE' not in json.dumps(payload)
    assert result['task_cards'][0]['diagnosis']['summary']=='PRIVATE_REPORT_PROSE'
    assert tools.egress_blocked==fail
    if not fail:
        await tools.check_egress()
        tools.egress_permits[0][1]['payload']['task_status']='failed'
        with pytest.raises(ValueError,match='binding_mismatch'):await tools.check_egress()


@pytest.mark.parametrize('collected',[False,True])
async def test_sensitive_legacy_history_is_not_resent(collected):
    seen=[]
    class Provider:
        async def complete(self,messages,schemas):seen.extend(messages);return {'role':'assistant','content':'已记录。'}
    cfg=SimpleNamespace(context_chars=50000,daily_gate_enabled=True,max_rounds=8,max_calls=16,total_timeout=30)
    engine=Engine(cfg,Provider(),SimpleNamespace(schemas=[]))
    previous=[{'model_messages':[{'role':'assistant','content':'PRIVATE_REPORT_PROSE'}],
        'response':{'task_cards':[{'infrastructure_evidence':[{'asset_id':'local-drill'}] if collected else [],'security_reviews':[{'decision':'deny'}]}]}}]
    result={'warnings':[],'model_called':False,'status':'answered'}
    await engine.run('解释原理',previous,'jwt',result)
    assert 'PRIVATE_REPORT_PROSE' not in json.dumps(seen)
