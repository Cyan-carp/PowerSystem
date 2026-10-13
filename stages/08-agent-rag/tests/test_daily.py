"""BND-M2: prove zero provider/tool calls, not just response flags."""
import json
from uuid import UUID, uuid4
import httpx
import pytest
from rag_agent.daily import classify, current_decision, model_history, SCOPE_REPLY, MIXED_REPLY
from rag_agent.main import create_app
from test_engine import config
from test_boundary import MemoryStore, request
from test_api_store import Redis, headers, environment


@pytest.mark.parametrize('question', [
    '西红柿炒蛋怎么做？', '番茄炒蛋的步骤', '炒鸡蛋', '推荐晚饭', '今天晚饭吃什么',
    '教我做菜', '推荐一家餐厅', '帮我规划旅游行程', '旅游攻略', '推荐一部电影',
    '讲个笑话', '陪我聊天', '我很无聊', '我失恋了', '制定健身计划', '明天天气怎么样',
    'Give me a dinner recipe', 'tell me a joke', 'recommend a movie', 'weather forecast',
])
def test_explicit_lifestyle_is_blocked(question):
    assert classify(question).action == 'block'


@pytest.mark.parametrize('question', [
    '什么是TCP？', '解释Redis', '你好，解释TCP', '设备日常巡检检查什么', '如何部署Docker',
    '第二台设备状态如何？', '它呢？', '帮我推荐数据库', '推荐一下', '怎么处理',
    'Python字符串包含“西红柿炒蛋怎么做”怎么处理',
    '用Python写一个菜谱推荐程序', '分析天气数据集的机器学习算法',
    '解释代码：`print("推荐晚饭")`', '推荐光伏技术资料',
])
def test_technical_and_unknown_requests_pass(question):
    assert classify(question).action == 'allow'


@pytest.mark.parametrize('question', ['解释Redis，再推荐晚饭', '解释TCP；讲个笑话', 'explain Redis, also recommend a movie', 'Redis推荐晚饭', '解释TCP，再回答“炒蛋怎么做”'])
def test_mixed_request_is_not_partially_sent_to_model(question):
    decision = classify(question)
    assert decision.action == 'block' and decision.reason == 'mixed' and decision.reply() == MIXED_REPLY


@pytest.mark.parametrize('question,reason', [('你好！','greeting'),(' ＨＥＬＬＯ！ ','greeting'),('谢谢你。','thanks'),('Thank you!','thanks'),('再见','farewell'),('你好，谢谢！','thanks'),('Hello, thank you!','thanks')])
def test_full_message_social_presets(question, reason):
    decision = classify(question)
    assert decision.action == 'preset' and decision.reason == reason


def turn(question, response=None, **extra):
    return {'question': question, 'response': response or {}, 'model_messages': [{'role':'user','content':question}], **extra}


def test_topic_continuation_and_display_history_filter():
    history = [turn('第二台设备'), turn('西红柿炒蛋怎么做'), turn('继续说'), turn('谢谢')]
    assert classify('具体步骤呢？', history).reason == 'daily_followup'
    assert classify('解释Redis', history).action == 'allow'
    assert model_history(history) == history[:1]
    assert model_history(history, False) is history
    history.append(turn('程序核验', kind='verification'))
    assert classify('继续说', history).action == 'block'
    history.append(turn('解释Redis'))
    assert classify('继续说', history).action == 'allow'


@pytest.mark.parametrize('value,expected', [('true',True),('1',True),('false',False),('0',False)])
def test_daily_configuration_switch(value,expected,monkeypatch,tmp_path):
    from rag_agent.config import Config
    key=tmp_path/'service-token';key.write_text('s'*32,encoding='utf-8')
    for name,setting in {'AGENT_SERVICE_TOKEN_FILE':str(key),'AGENT_LLM_MODEL':'test','AGENT_LLM_BASE_URL':'https://model.example',
                         'RAG_DATABASE_URL':'postgresql://test','AGENT_REDIS_URL':'redis://test','BND_DAILY_GATE_ENABLED':value}.items():
        monkeypatch.setenv(name,setting)
    assert Config.load().daily_gate_enabled is expected


def test_invalid_daily_configuration_is_rejected(monkeypatch,tmp_path):
    from rag_agent.config import Config
    key=tmp_path/'service-token';key.write_text('s'*32,encoding='utf-8')
    for name,setting in {'AGENT_SERVICE_TOKEN_FILE':str(key),'AGENT_LLM_MODEL':'test','AGENT_LLM_BASE_URL':'https://model.example',
                         'RAG_DATABASE_URL':'postgresql://test','AGENT_REDIS_URL':'redis://test','BND_DAILY_GATE_ENABLED':'maybe'}.items():
        monkeypatch.setenv(name,setting)
    with pytest.raises(ValueError,match='daily_gate_enabled'): Config.load()


class Provider:
    def __init__(self, forbidden=False): self.requests = []; self.forbidden = forbidden
    async def complete(self, messages, schemas=None):
        self.requests.append(messages)
        if self.forbidden: pytest.fail('blocked/preset requests must never call the provider')
        return {'role':'assistant','content':'这是技术解释。'}


class NoTools:
    schemas = []
    def __init__(self): self.calls = []
    async def execute(self, *args):
        self.calls.append(args)
        pytest.fail('program replies must never invoke business/search/embedding tools')


@pytest.mark.asyncio
@pytest.mark.parametrize('question,mode,status', [
    ('你好', 'preset', 'answered'), ('谢谢', 'preset', 'answered'), ('再见', 'preset', 'answered'),
    ('西红柿炒蛋怎么做', 'boundary', 'out_of_scope'), ('推荐晚饭', 'boundary', 'out_of_scope'),
    ('规划旅行行程', 'boundary', 'out_of_scope'), ('讲个笑话', 'boundary', 'out_of_scope'),
    ('解释Redis，再推荐晚饭', 'boundary', 'out_of_scope'),
])
async def test_program_reply_no_calls_saved_and_no_verification(question, mode, status, tmp_path):
    cfg=config(); cfg.audit_dir=tmp_path
    store=MemoryStore(); redis=Redis(); provider=Provider(True); tools=NoTools()
    app=create_app(cfg,store,redis,provider,tools)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        out=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':question})
        assert out.status_code==200
        data=out.json(); sid=data['session_id']
        assert data['status']==status and data['response_mode']==mode and data['model_called'] is False
        assert data['model']=='' and data['sources']==[] and data['tool_calls']==[]
        assert data['verification_available'] is False and data['evidence']['state']=='not_queried'
        assert data['turn_id']==1 and len(store.rows)==1 and store.rows[0]['model_messages']==[]
        assert not provider.requests and not tools.calls and not redis.locks
        saved=(await client.get(f'/internal/agent/sessions/{sid}?user_id=17',headers=headers(cfg))).json()['turns'][0]
        assert saved['response']['verification_available'] is False
        denied=await client.post(f'/internal/agent/sessions/{sid}/verify',headers=headers(cfg),json=request())
        assert denied.status_code==400 and not tools.calls
        foreign=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':18,'message':question,'session_id':sid})
        assert foreign.status_code==404 and len(store.rows)==1 and not provider.requests
        audit=json.loads(next(tmp_path.glob('*.jsonl')).read_text(encoding='utf-8').splitlines()[0])
        assert audit['model_called'] is False and audit['boundary']['action'] in ('preset','block')


@pytest.mark.asyncio
async def test_multi_turn_block_then_technical_context_and_switch(tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;store=MemoryStore();provider=Provider();redis=Redis();tools=NoTools()
    app=create_app(cfg,store,redis,provider,tools)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        async def send(q, sid=None):
            reply=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':q,**({'session_id':sid} if sid else {})})
            assert reply.status_code==200
            return reply.json()
        first=await send('炒蛋怎么做');sid=first['session_id']
        await send('谢谢',sid)
        follow=await send('继续说',sid)
        assert follow['status']=='out_of_scope' and not provider.requests
        tech=await send('你好，解释TCP',sid)
        assert tech['model_called'] is True and tech['verification_available'] is True
        assert '炒蛋' not in str(provider.requests) and '谢谢' not in str(provider.requests)
        unknown=await send('推荐一下',sid)
        assert unknown['model_called'] is True and unknown['boundary']['reason']=='uncertain'
        cfg.daily_gate_enabled=False
        old=await send('西红柿炒蛋怎么做',sid)
        assert old['response_mode']=='model' and old['model_called'] is True and 'boundary' not in old
        health=(await client.get('/health')).json()
        assert health['boundaries']['daily_gate'] is False


@pytest.mark.asyncio
async def test_classifier_failure_never_falls_through(monkeypatch,tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;provider=Provider(True);redis=Redis()
    def fail(*args): raise RuntimeError('private internal detail')
    monkeypatch.setattr('rag_agent.main.classify',fail)
    app=create_app(cfg,MemoryStore(),redis,provider,NoTools())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        reply=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':'你好'})
        assert reply.status_code==503 and 'private' not in reply.text and not provider.requests and not redis.locks


@pytest.mark.asyncio
async def test_program_records_survive_postgres_restart(environment):
    cfg,store,user=environment;cfg.daily_gate_enabled=True
    cfg.audit_dir=cfg.audit_dir/'daily-tests'
    provider=Provider(True);app=create_app(cfg,store,Redis(),provider,NoTools())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        data=(await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':user,'message':'推荐晚饭'})).json()
        sid=data['session_id']
    from rag_agent.store import Store
    reopened=await Store.open(cfg.database_url)
    try:
        saved=await reopened.turns(UUID(sid))
        assert saved[0]['response']['answer_markdown']==SCOPE_REPLY and saved[0]['model_messages']==[]
        next_app=create_app(cfg,reopened,Redis(),provider,NoTools())
        async with next_app.router.lifespan_context(next_app),httpx.AsyncClient(transport=httpx.ASGITransport(app=next_app),base_url='http://test') as client:
            follow=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':user,'message':'具体步骤呢','session_id':sid})
            assert follow.json()['status']=='out_of_scope' and not provider.requests
    finally: await reopened.pool.close()


@pytest.mark.asyncio
async def test_program_reply_security_busy_and_storage_failure(tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;provider=Provider(True);redis=Redis();store=MemoryStore()
    sid=uuid4();await store.ensure(sid,17,'busy',True)
    redis.locks['stage8:session:'+str(sid)]='occupied'
    app=create_app(cfg,store,redis,provider,NoTools())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        body={'user_id':17,'message':'你好','session_id':str(sid)}
        assert (await client.post('/internal/agent/chat',json=body)).status_code==401
        assert (await client.post('/internal/agent/chat',headers={'X-PowerSystem-Token':cfg.service_token},json=body)).status_code==401
        assert (await client.post('/internal/agent/chat',headers=headers(cfg),json=body)).status_code==409
        assert not provider.requests and not store.rows
    class FailingStore(MemoryStore):
        async def save(self,*args,**kwargs): raise RuntimeError('storage unavailable')
    redis=Redis();app=create_app(cfg,FailingStore(),redis,provider,NoTools())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://test') as client:
        reply=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':'你好'})
        assert reply.status_code==500 and not provider.requests and not redis.locks


@pytest.mark.asyncio
async def test_attempted_model_failure_is_recorded(tmp_path):
    from rag_agent.provider import ModelError
    class Broken:
        async def complete(self,*args): raise ModelError('primary unavailable')
    cfg=config();cfg.audit_dir=tmp_path
    app=create_app(cfg,MemoryStore(),Redis(),Broken(),NoTools())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        blocked=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':'推荐晚饭'})
        assert blocked.json()['status']=='out_of_scope' and blocked.json()['model_called'] is False
        failed=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':'解释TCP'})
        assert failed.json()['status']=='degraded' and failed.json()['model_called'] is True
