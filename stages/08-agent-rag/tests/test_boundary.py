import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4, UUID
import httpx
import pytest
from rag_agent.evidence import build_evidence
from rag_agent.main import create_app
from rag_agent.tools import Tools
from rag_agent.provider import ModelError
from test_engine import config
from test_api_store import Redis, Provider, headers, environment


class MemoryStore:
    def __init__(self): self.users = {}; self.rows = []
    async def ensure(self, sid, user, title, create=False):
        if create: self.users[sid] = user
        return self.users.get(sid) == user
    async def turns(self, sid): return [r for r in self.rows if r['sid'] == sid]
    async def verification(self, sid, vid):
        return next((r for r in self.rows if r['sid'] == sid and r.get('verification_id') == vid), None)
    async def save(self, sid, question, response, messages, **meta):
        row = {'id': len(self.rows)+1, 'sid': sid, 'question': question, 'response': json.loads(json.dumps(response)), 'model_messages': messages, **meta}
        self.rows.append(row)
        return row['id']
    async def list(self, user, offset=0): return []
    async def delete(self, sid, user):
        if self.users.get(sid) != user: return False
        self.users.pop(sid); self.rows = [r for r in self.rows if r['sid'] != sid]; return True


def request(parent=1, device=2, **extra):
    return {'user_id': 17, 'parent_turn_id': parent, 'device_id': device,
            'checks': ['latest_telemetry', 'current_alarms'], 'verification_id': str(uuid4()), **extra}


def backend(calls, alarm_fail=False, many=False, invalid=False):
    def handler(req):
        calls.append(str(req.url))
        assert req.method == 'GET' and req.headers['Authorization'] == 'Bearer test-user-token'
        if req.url.path == '/api/v1/devices/2':
            if invalid: return httpx.Response(404, json={'code': 40401})
            data = {'id':2, 'device_code':'INV-1002', 'name':'第二台'}
        elif req.url.path.endswith('/telemetry/latest'):
            data = {'device_id':2, 'device_code':'INV-1002', 'ts_ms':1000, 'received_at':'2026-10-11T03:01:22.037589Z', 'voltage':400.93, 'status':1}
        elif req.url.path.endswith('/prediction'):
            data = {'device_id':2, 'window_end_ms':1000, 'stale':True, 'probability':.9}
        else:
            if alarm_fail: return httpx.Response(503, json={'code':50301})
            status = req.url.params['status']; page = int(req.url.params['page'])
            assert status in ('unhandled','acked') and req.url.params['device_id'] == '2'
            count = 100 if many else 0
            data = {'list':[{'id':page*100+i, 'device_id':2, 'status':status} for i in range(count)], 'total':250 if many else 0}
        return httpx.Response(200,json={'code':0,'data':data})
    return handler


@pytest.mark.parametrize('age,expected', [(120,'fresh'),(120.001,'stale'),(-60,'fresh'),(-60.001,'future')])
def test_freshness_exact_boundaries(age, expected):
    now = datetime(2026,10,11,tzinfo=timezone.utc)
    call = {'name':'get_telemetry','status':'ok','arguments':{'device_id':2},'collected_at':now.isoformat(),'result':{'ts_ms':(now-timedelta(seconds=age)).timestamp()*1000,'received_at':now.isoformat()}}
    card=build_evidence([call],config())['cards'][0]
    assert card['freshness']==expected and card['received_at']==now.isoformat()


def test_real_calls_only_and_unknown_timestamps():
    assert build_evidence([{'name':'web_search','status':'ok'}],config())['state']=='not_queried'
    result=build_evidence([{'name':'get_telemetry','status':'ok','result':{'ts_ms':'bad'}},{'name':'get_prediction','status':'error'}],config())
    assert result['state']=='partial_failed' and result['cards'][0]['freshness']=='unknown'
    assert build_evidence([{'name':'get_prediction','status':'ok','result':{'stale':True}}],config())['cards'][0]['freshness']=='stale'


@pytest.mark.asyncio
@pytest.mark.parametrize('question',['第二台设备状态如何？','帮我看看第二台现在怎么样','第二台有没有异常？','它呢？','西红柿炒蛋怎么做？'])
async def test_zero_calls_no_automatic_query(question, tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;store=MemoryStore();calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(calls))) as business:
        app=create_app(cfg,store,Redis(),Provider(),Tools(cfg,business,None))
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            out=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':question})
            assert out.status_code==200 and out.json()['evidence']['state']=='not_queried'
            assert out.json()['turn_id']==1 and out.json()['tool_calls']==[] and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize('alarm_fail,many',[(False,False),(True,False),(False,True)])
async def test_explicit_verification_records_partial_and_idempotent(alarm_fail,many,tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;cfg.verify_max_pages=1
    store=MemoryStore();sid=uuid4();await store.ensure(sid,17,'test',True)
    original={'answer_markdown':'原回答不修改','tool_calls':[]}
    await store.save(sid,'第二台',original,[]);calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(calls,alarm_fail,many))) as business:
        app=create_app(cfg,store,Redis(),Provider(),Tools(cfg,business,None))
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            body=request(checks=['prediction','current_alarms','latest_telemetry'])
            url=f'/internal/agent/sessions/{sid}/verify'
            first=await client.post(url,headers=headers(cfg),json=body)
            assert first.status_code==200,first.text
            result=first.json();count=len(calls)
            assert result['turn_id']==2 and result['kind']=='verification' and result['parent_turn_id']==1
            assert result['verification_results']['current_alarms']['complete'] is (not alarm_fail and not many)
            assert result['evidence']['cards'][-1]['freshness']=='stale'
            retry=await client.post(url,headers=headers(cfg),json=body)
            assert retry.json()['turn_id']==2 and len(calls)==count and len(store.rows)==2
            assert store.rows[0]['response']==original
            conflict=await client.post(url,headers=headers(cfg),json={**body,'checks':['prediction']})
            assert conflict.status_code==409 and len(calls)==count
            for payload,code in [({**body,'user_id':18},404),({**body,'parent_turn_id':999},404),({**body,'checks':['control']},422)]:
                assert (await client.post(url,headers=headers(cfg),json=payload)).status_code==code
            assert len(calls)==count
            cfg.verify_enabled=False
            assert (await client.post(url,headers=headers(cfg),json=body)).status_code==503


@pytest.mark.asyncio
async def test_busy_delete_invalid_device_and_model_independence(tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;store=MemoryStore();sid=uuid4();await store.ensure(sid,17,'x',True);await store.save(sid,'x',{},[])
    redis=Redis();calls=[]
    class Broken:
        async def complete(self,*args): raise ModelError('primary unavailable')
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(calls,invalid=True))) as business:
        app=create_app(cfg,store,redis,Broken(),Tools(cfg,business,None))
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            out=await client.post('/internal/agent/chat',headers=headers(cfg),json={'user_id':17,'message':'技术问题','session_id':str(sid)})
            assert out.json()['status']=='degraded' and not out.json()['answer_markdown']
            url=f'/internal/agent/sessions/{sid}/verify'
            assert (await client.post(url,headers=headers(cfg),json=request())).status_code==400
            assert len(calls)==1
            redis.locks['stage8:session:'+str(sid)]='busy'
            assert (await client.post(url,headers=headers(cfg),json=request())).status_code==409
            assert (await client.delete(f'/internal/agent/sessions/{sid}?user_id=17',headers=headers(cfg))).status_code==409


@pytest.mark.asyncio
async def test_postgres_verification_persists_after_new_app(environment):
    cfg,store,user=environment;sid=uuid4();vid=uuid4();await store.ensure(sid,user,'核验持久化',True)
    parent=await store.save(sid,'original',{'answer_markdown':'保留原文'},[])
    out={'verification_request':{'parent_turn_id':parent,'device_id':2,'checks':['prediction']},'verification_results':{'prediction':{'status':'ok','data':{'stale':True}}},'tool_calls':[]}
    child=await store.save(sid,'verify',out,[],kind='verification',parent_turn_id=parent,verification_id=vid)
    from rag_agent.store import Store
    restarted=await Store.open(cfg.database_url)
    try:
        saved=await restarted.verification(sid,vid)
        assert saved['id']==child and saved['response']==out
        turns=await restarted.turns(sid)
        assert turns[0]['response']['answer_markdown']=='保留原文' and turns[1]['parent_turn_id']==parent and turns[1]['kind']=='verification'
    finally: await restarted.pool.close()


@pytest.mark.asyncio
async def test_verification_succeeds_without_primary_and_evidence_switch(tmp_path):
    cfg=config();cfg.audit_dir=tmp_path;cfg.evidence_enabled=False
    store=MemoryStore();sid=uuid4();await store.ensure(sid,17,'x',True);await store.save(sid,'x',{'tool_calls':[]},[])
    class Broken:
        async def complete(self,*args): pytest.fail('verification must not use any model')
    calls=[]
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend(calls))) as business:
        app=create_app(cfg,store,Redis(),Broken(),Tools(cfg,business,None))
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            result=await client.post(f'/internal/agent/sessions/{sid}/verify',headers=headers(cfg),json=request())
            assert result.status_code==200 and result.json()['status']=='answered' and len(calls)==4 and 'evidence' not in result.json()
            history=await client.get(f'/internal/agent/sessions/{sid}?user_id=17',headers=headers(cfg))
            assert all('evidence' not in t['response'] for t in history.json()['turns'])
