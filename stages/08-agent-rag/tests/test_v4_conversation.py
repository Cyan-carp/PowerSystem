"""Q01–Q10 S02 scope: natural text, targets, historical tasks and truthful gaps."""
import json
from types import SimpleNamespace
from uuid import uuid4
import pytest
from rag_agent.engine import Engine
from rag_agent.config import Config
from rag_agent.provider import ModelError
from rag_agent import v4


def cfg(enabled=True):
    value = Config('fixture', 'https://example.invalid/v1', 'fixture', 's'*32, 'pg', 'redis')
    value.v4_assistant_enabled = enabled
    return value


def output():
    return dict(status='degraded', answer_markdown='', tool_calls=[], sources=[], warnings=[])


class Provider:
    def __init__(self, replies): self.replies, self.requests = list(replies), []
    async def complete(self, messages, schemas):
        self.requests.append(messages.copy())
        item = self.replies.pop(0)
        if isinstance(item, Exception): raise item
        return item


def reply(text): return {'role': 'assistant', 'content': text}


@pytest.mark.asyncio
@pytest.mark.parametrize('question,answer', [
    ('设备日常巡检需要检查什么', '检查外观、接线及运行记录；按资料适用范围制定清单。[巡检资料](rag://K123)'),
    ('这两句话什么意思：证据不足，不能报告设备状态', '它表示资料还不足以支持现场判断，需要补充证据。'),
    ('这两句话什么意思：“这台设备证据不足，不能报告当前状态”', '这是说明证据不足，不能据此断言正常。'),
    ('Redis是什么，晚餐可以吃什么', 'Redis是内存数据库。晚餐可以选择米饭、蔬菜和蛋白质。'),
    ('谢谢', '不客气！'),
])
async def test_natural_answers_are_not_overwritten(question, answer):
    result = output(); p = Provider([reply(answer)])
    await Engine(cfg(), p, SimpleNamespace(schemas=[])).run(question, [], 'jwt', result)
    assert result['answer_markdown'] == answer
    assert 'business_quality' not in result
    assert result['conversation_context']['objective'] == question


@pytest.mark.asyncio
async def test_knowledge_hit_with_device_word_does_not_force_live_evidence():
    class Tools:
        schemas=[]
        async def execute(self, name, args, jwt, result):
            result['tool_calls'].append({'name':name, 'status':'ok', 'result':{'results':[{'id':'K123','version':'hash1'}]}})
            return {'results':[{'id':'K123','text':'检查外观和接线','version':'hash1'}]}
    p = Provider([{'role':'assistant','content':None,'tool_calls':[{'id':'k','function':{'name':'search_knowledge','arguments':'{"query":"日常巡检"}'}}]}, reply('按资料K123检查外观和接线。')])
    result = output()
    await Engine(cfg(), p, Tools()).run('设备日常巡检需要检查什么', [], 'jwt', result)
    assert result['answer_markdown'] == '按资料K123检查外观和接线。'
    assert 'business_facts' not in p.requests[-1][-1]['content']


@pytest.mark.asyncio
async def test_failed_task_is_retrievable_after_reloading_turns_not_reexecuted():
    tid=str(uuid4())
    saved = {'question':'第二台设备近7天汇总发电量', 'response':{'task_cards':[{'task_id':tid,'status':'failed'}]}, 'model_messages':[reply('旧错误正文')]}
    # JSON roundtrip represents persisted response/model_messages, not process memory.
    previous=json.loads(json.dumps([saved]))
    class Tools:
        schemas=[]
        async def execute(self,name,args,jwt,result):
            assert name == 'get_ops_task' and json.loads(args)['task_id']==tid
            card={'task_id':tid,'status':'failed','historical':True,'error':{'code':'required_tool_failed'},
                'tool_calls':[{'name':'platform.get_telemetry','status':'error','error':'HTTP 503'}]}
            result['task_cards']=[card]
            result['tool_calls'].append({'name':name,'status':'ok','arguments':json.loads(args)})
            return card
    p=Provider([{'role':'assistant','content':None,'tool_calls':[{'id':'t','function':{'name':'get_ops_task','arguments':json.dumps({'task_id':tid})}}]},reply('没有任务')])
    result=output()
    await Engine(cfg(),p,Tools()).run('上述任务失败原因是什么',previous,'jwt',result)
    assert tid in json.dumps(p.requests[0]) and '近7天' in json.dumps(p.requests[0],ensure_ascii=False)
    assert 'HTTP 503' in result['answer_markdown'] and 'required_tool_failed' in result['answer_markdown']
    assert '没有任务' not in result['answer_markdown']


def test_latest_one_objective_is_complete_but_full_list_is_not():
    call={'name':'list_alarms','status':'ok','collected_at':'2026-10-23T02:00:00Z','arguments':{'page':1,'page_size':1},
        'result':{'list':[{'id':7,'device_id':2,'status':'recovered','triggered_at':'2026-10-23T01:00:00Z'}],'total':3}}
    result=output();result['tool_calls']=[call]
    assert v4.finish('全部设备最新一条告警',result,[])
    assert result['objective_quality']['complete'] is True and '7' in result['answer_markdown']
    assert '分页不完整' not in result['answer_markdown']
    result=output();result['tool_calls']=[{**call,'collected_at':'2026-10-23T02:00:00Z'}]
    v4.finish('全部设备所有告警',result,[])
    assert '告警分页不完整' in result['business_quality']['missing']


@pytest.mark.asyncio
async def test_model_outage_explicit_and_no_fabricated_success():
    result=output()
    await Engine(cfg(),Provider([ModelError('模型服务异常（HTTP 503）')]),SimpleNamespace(schemas=[])).run('解释设备手册',[],'jwt',result)
    assert result['status']=='degraded' and 'HTTP 503' in result['answer_markdown']


@pytest.mark.asyncio
async def test_current_queries_never_replay_sensitive_reports():
    secret='PRIVATE_REPORT_PROSE'
    previous=[{'question':'第二台设备状态', 'response':{'task_cards':[{'task_id':str(uuid4()),'status':'failed','security_reviews':[{}]}]},'model_messages':[reply(secret)]}]
    p=Provider([reply('当前状态需要重新查询。')]);result=output()
    await Engine(cfg(),p,SimpleNamespace(schemas=[])).run('这两句话什么意思',previous,'jwt',result)
    assert secret not in json.dumps(p.requests)
    assert '历史目标仅用于指代' in json.dumps(p.requests,ensure_ascii=False)


def test_context_targets_and_capabilities_are_explicit():
    prompt=v4.prompt([{'function':{'name':'get_ops_task'}}])
    for required in ('id ASC','energy_daily','24小时遥测不能替代','尚无产物','get_ops_task','实时天气'):
        assert required in prompt
    assert v4.intent('这两句话什么意思')=='explanation'
    assert v4.intent('现在第二台设备的状态如何')!='explanation'


def test_availability_export_reports_actual_adapter_gap():
    result=output();result['answer_markdown']='可用率就是在线率，已导出'
    v4.finish('导出今天设备可用率报表',result,[])
    assert result['objective_quality']['complete'] is False
    assert '未生成文件' in result['answer_markdown'] and '在线率' in result['answer_markdown']


def test_capability_answer_comes_from_runtime_registration():
    result=output();result['answer_markdown']='可以导出文件并修改业务'
    result['capability_catalog']={'registered_tools':['search_knowledge','get_ops_task']}
    v4.finish('你可以做什么',result,[])
    assert '检索项目知识资料' in result['answer_markdown'] and '本人的历史任务' in result['answer_markdown']
    assert '尚未接入' in result['answer_markdown'] and '可以导出文件并修改业务' not in result['answer_markdown']


@pytest.mark.asyncio
async def test_evidence_repair_executes_requested_tools_before_correction():
    class Tools:
        schemas=[{'function':{'name':'dispatch_ops_task'}}]
        def __init__(self):self.executed=[]
        async def execute(self,name,args,jwt,result):
            self.executed.append(name)
            result['tool_calls'].append({'name':name,'status':'ok','arguments':{}})
            return {'ok':True}
    def tool(name,id):return {'role':'assistant','content':None,'tool_calls':[{'id':id,'function':{'name':name,'arguments':'{}'}}]}
    provider=Provider([tool('search_knowledge','k'),reply('请提供设备编号'),tool('dispatch_ops_task','d'),reply('已查询')])
    tools=Tools();result=output()
    await Engine(cfg(),provider,tools).run('第二台设备状态如何',[],'jwt',result)
    assert tools.executed==['search_knowledge','dispatch_ops_task']
    assert len(provider.requests)==4
    for request in provider.requests:
        waiting=set()
        for msg in request:
            if msg.get('tool_calls'):waiting.update(c['id'] for c in msg['tool_calls'])
            elif msg['role']=='tool':waiting.remove(msg['tool_call_id'])
            else:assert not waiting
        assert not waiting


@pytest.mark.asyncio
async def test_missing_current_evidence_is_marked_after_bounded_repair():
    provider=Provider([reply('请提供设备编号'),reply('缺少台账')]);result=output()
    await Engine(cfg(),provider,SimpleNamespace(schemas=[{'function':{'name':'dispatch_ops_task'}}])).run('第二台设备状态如何',[],'jwt',result)
    assert len(provider.requests)==2
    assert result['objective_quality']=={'complete':False,'reason':'evidence_missing'}
    assert '未取得平台业务证据' in result['answer_markdown']


def test_legacy_task_tool_result_without_card_is_not_replayed():
    block=v4.history_block({'question':'失败原因','response':{'tool_calls':[{'name':'get_ops_task','status':'error'}]},'model_messages':[reply('OLD_REPORT')]})
    assert 'OLD_REPORT' not in str(block)
    assert v4.history_block({'model_messages':None})==[]


@pytest.mark.asyncio
async def test_v4_http_gate_and_history_reload_with_file_fixture(tmp_path):
    """HTTP wiring fixture; explicitly not a PostgreSQL restart acceptance."""
    import httpx
    from rag_agent.main import create_app
    from test_api_store import Redis
    from rag_agent.tools import Tools
    path=tmp_path/'sessions.json'
    class FileStore:
        def __init__(self):self.rows=json.loads(path.read_text()) if path.exists() else {}
        async def ensure(self,sid,user,title,create=False):
            if create:self.rows[str(sid)]={'user':user,'turns':[]}
            return self.rows.get(str(sid),{}).get('user')==user
        async def turns(self,sid):return self.rows[str(sid)]['turns']
        async def save(self,sid,question,response,messages):
            rows=self.rows[str(sid)]['turns'];tid=len(rows)+1
            rows.append({'id':tid,'question':question,'response':response,'model_messages':messages,'kind':'chat'})
            path.write_text(json.dumps(self.rows),encoding='utf-8')
            return tid
    config=cfg();config.audit_dir=tmp_path/'audit'
    headers={'X-PowerSystem-Token':config.service_token,'Authorization':'Bearer synthetic-user'}
    provider=Provider([reply('Redis解释和晚餐建议。')])
    app=create_app(config,FileStore(),Redis(),provider,Tools(config,None,None))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/internal/agent/chat',headers=headers,json={'user_id':1,'message':'解释Redis，再推荐晚餐'})
        result=response.json();sid=result['session_id']
        assert result['status']=='answered' and result['answer_markdown']=='Redis解释和晚餐建议。'
        assert (await client.get('/health')).json()['boundaries']['v4_assistant'] is True
    follow=Provider([reply('这是对刚才文字的解释。')])
    app=create_app(config,FileStore(),Redis(),follow,Tools(config,None,None))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        response=await client.post('/internal/agent/chat',headers=headers,json={'user_id':1,'message':'这两句话什么意思','session_id':sid})
        assert response.json()['status']=='answered'
        assert 'Redis解释和晚餐建议' in str(follow.requests)
        denied=await client.post('/internal/agent/chat',headers=headers,json={'user_id':2,'message':'继续','session_id':sid})
        assert denied.status_code==404


@pytest.mark.asyncio
async def test_comparison_reads_both_original_tasks_and_calculates_from_facts():
    from rag_agent.business_quality import build_quality
    tids=[str(uuid4()),str(uuid4())]
    def task(tid,did,power):
        stamp='2026-10-23T02:00:00Z'
        calls=[{'name':'get_device','status':'ok','arguments':{'device_id':did},'result':{'id':did,'device_code':'SYN-'+str(did)},'collected_at':stamp},
            {'name':'get_telemetry','status':'ok','arguments':{'device_id':did},'result':{'device_id':did,'power':power,'status':1,'ts_ms':1792720800000,'received_at':stamp},'collected_at':stamp}]
        return {'task_id':tid,'status':'succeeded','historical':True,'business_quality':build_quality(calls)}
    tasks={tids[0]:task(tids[0],28,512.4),tids[1]:task(tids[1],11,256.2)}
    previous=[{'question':q,'response':{'task_cards':[tasks[tid]]},'model_messages':[]} for q,tid in zip(['第二台设备状态','第一台设备状态'],tids)]
    class Tools:
        schemas=[{'function':{'name':'get_ops_task'}},{'function':{'name':'dispatch_ops_task'}}]
        async def execute(self,name,args,jwt,result):
            assert name=='get_ops_task'
            card=tasks[json.loads(args)['task_id']]
            result.setdefault('task_cards',[]).append(card)
            return card
    calls=[{'id':str(i),'function':{'name':'get_ops_task','arguments':json.dumps({'task_id':tid})}} for i,tid in enumerate(tids)]
    p=Provider([{'role':'assistant','content':None,'tool_calls':calls},reply('两者相同')]);result=output()
    await Engine(cfg(),p,Tools()).run('这两台设备有什么差异',previous,'jwt',result)
    assert result['objective_quality']['complete'] is True
    assert '512.4' in result['answer_markdown'] and '256.2' in result['answer_markdown']
    assert '相差 256.2 kW' in result['answer_markdown'] and '历史' in result['answer_markdown']


def test_comparison_incomplete_is_not_reported_as_complete():
    tids=[str(uuid4()),str(uuid4())]
    previous=[{'response':{'task_cards':[{'task_id':tid,'status':'succeeded'}]}} for tid in tids]
    result=output();result['answer_markdown']='两台相同'
    v4.finish('这两台设备有什么差异',result,previous)
    assert result['objective_quality']['complete'] is False
    assert '比较尚未完成' in result['answer_markdown']
