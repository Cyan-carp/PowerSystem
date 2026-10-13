import json
from pathlib import Path
import httpx
import pytest
from rag_agent.config import Config
from rag_agent.engine import Engine, SYSTEM
from rag_agent.provider import ModelError
from rag_agent.tools import Tools, public_link


def config():
    return Config("test", "https://model.example", "private-model-key", "s" * 32, "postgresql://private", "redis://private")


def response():
    return {"status": "degraded", "answer_markdown": "", "sources": [], "tool_calls": [], "warnings": []}


@pytest.mark.asyncio
async def test_pending_delegation_returns_card_without_second_model_call():
    class PendingTools:
        schemas=[]
        async def execute(self,name,args,jwt,result):
            result['response_mode']='task_dispatched'
            result['task_cards']=[{'task_id':'fixture','status':'planning'}]
            return result['task_cards'][0]
    provider=Provider([{'role':'assistant','content':None,'tool_calls':[{'id':'dispatch','function':{'name':'dispatch_ops_task','arguments':'{}'}}]}])
    result=response()
    messages=await Engine(config(),provider,PendingTools()).run('查设备',[],'jwt',result)
    assert len(provider.requests)==1 and result['status']=='answered'
    assert '受理成功不代表查询完成' in result['answer_markdown'] and messages[-1]['role']=='assistant'


@pytest.mark.asyncio
async def test_delegation_wait_respects_remaining_budget():
    import asyncio,time
    from rag_agent.ops import Ops
    class SlowOps:
        async def get(self,tid):await asyncio.sleep(1);return {'task_id':tid,'status':'queued'}
    started=time.monotonic()
    value=await Ops.wait(SlowOps(),{'task_id':'fixture','status':'queued'},.02)
    assert time.monotonic()-started<.2 and value['status']=='queued'


class Provider:
    def __init__(self, replies): self.replies, self.requests = list(replies), []
    async def complete(self, messages, schemas=None):
        self.requests.append((messages.copy(), schemas))
        item = self.replies.pop(0)
        if isinstance(item, Exception): raise item
        return item


@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["解释Redis持久化", "什么是TCP？", "给我写一篇技术文章"])
async def test_free_answer_without_required_sources(question):
    text = "普通自然语言答案。" * 300
    provider = Provider([{"role": "assistant", "content": text}])
    tools = Tools(config(), None, None)
    out = response()
    messages = await Engine(config(), provider, tools).run(question, [], "jwt", out)
    assert out["status"] == "answered" and out["answer_markdown"] == text
    assert out["sources"] == [] and out["tool_calls"] == []
    assert messages[-1]["content"] == text
    assert "submit_plan" not in json.dumps(provider.requests, ensure_ascii=False)


@pytest.mark.asyncio
async def test_tool_failure_still_allows_general_answer():
    provider = Provider([{"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "web_search", "arguments": '{"query":"新闻"}'}}]},
                         {"role": "assistant", "content": "搜索未配置，我可以解释一般背景。"}])
    out = response()
    async with httpx.AsyncClient() as client:
        await Engine(config(), provider, Tools(config(), client, None)).run("新闻", [], "jwt", out)
    assert out["status"] == "answered" and out["tool_calls"][0]["status"] == "error"
    assert out["warnings"] and out["sources"] == []


@pytest.mark.asyncio
async def test_model_failure_is_not_answered():
    out = response()
    messages = await Engine(config(), Provider([ModelError("模型故障")]), Tools(config(), None, None)).run("解释技术", [], "jwt", out)
    assert out["status"] == "degraded" and not out["answer_markdown"]
    assert out["warnings"] == ["模型故障"] and "服务故障" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_empty_knowledge_can_answer_without_inventing_document_sources():
    class EmptyKnowledge:
        async def search(self, query, limit): return []
    provider = Provider([
        {"role":"assistant","content":None,"tool_calls":[{"id":"c1","function":{"name":"search_knowledge","arguments":'{"query":"未入库的技术资料"}'}}]},
        {"role":"assistant","content":"内部资料未找到相关内容，我可以介绍通用背景。"}])
    out = response()
    await Engine(config(),provider,Tools(config(),None,EmptyKnowledge())).run("技术问题",[],"jwt",out)
    assert out['status']=='answered' and out['sources']==[]
    assert out['tool_calls'][0]['status']=='ok'
    assert "通用背景" in out['answer_markdown']


@pytest.mark.asyncio
async def test_business_data_not_filtered_for_age_or_numbers():
    def handler(req):
        assert req.method == "GET" and req.headers["authorization"] == "Bearer user-token"
        return httpx.Response(200, json={"code": 0, "data": {"device_id": 2, "ts_ms": 1000, "power": 123.456}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = response()
        data = await Tools(config(), client, None).execute("get_telemetry", '{"device_id":2}', "user-token", out)
    assert data["ts_ms"] == 1000 and data["power"] == 123.456
    assert out["sources"][0]["kind"] == "business" and out["tool_calls"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_unknown_write_tool_and_invalid_device_never_execute():
    def handler(req): pytest.fail("invalid tools must never send HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        tools = Tools(config(), client, None)
        for name, args in [("ack_alarm", '{"alarm_id":1}'), ("get_prediction", '{"device_id":-1}')]:
            out = response(); data = await tools.execute(name, args, "jwt", out)
            assert "error" in data and not out["sources"]


@pytest.mark.asyncio
async def test_web_query_unrestricted_and_querystring_url_supported():
    cfg = config(); cfg.search_key = "bocha-private"
    def handler(req):
        assert json.loads(req.content)["query"] == "现在西红柿炒蛋做法"
        return httpx.Response(200, json={"code":200,"data":{"webPages":{"value":[{"name":"食谱","url":"https://example.com/read?id=123","summary":"做法","datePublished":"2026-10-11"}]}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        out = response(); data = await Tools(cfg, client, None).execute("web_search", '{"query":"现在西红柿炒蛋做法"}', "jwt", out)
    assert data["results"][0]["url"].endswith("?id=123") and out["sources"][0]["kind"] == "web"
    assert not public_link("javascript:alert(1)")


@pytest.mark.asyncio
async def test_history_whole_turns_and_multi_turn_reference():
    previous = [{"model_messages": [{"role":"user","content":"第二台设备"},{"role":"assistant","content":"INV-1002"}]}]
    provider = Provider([{"role":"assistant","content":"它是INV-1002"}])
    out = response()
    await Engine(config(), provider, Tools(config(), None, None)).run("它呢", previous, "jwt", out)
    assert provider.requests[0][0][2]["content"] == "INV-1002"


def test_no_legacy_rules_or_imports():
    root = Path(__file__).resolve().parents[1] / "rag_agent"
    assert "只读运维助手" not in SYSTEM and "范围外" not in SYSTEM
    source = "\n".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    for old in ("submit_plan", "submit_support_review", "validate_answer", "PUBLIC_TERMS", "from agent."):
        assert old not in source
