import json
import httpx
import pytest
from rag_agent.config import Config
from rag_agent.provider import ModelError, Provider, merge_tool_calls, parse_event_stream


def config():
    return Config("deepseek-v4.1-flash", "https://model.example/v1", "private-model-key", "s" * 32,
                  "postgresql://private", "redis://private")


def sse(*chunks):
    lines = [": keepalive", ""]
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def delta(content=None, **extra):
    body = {"choices": [{"index": 0, "delta": {"content": content, **extra}, "finish_reason": None}]}
    return body


def transport(body, status=200, content_type="text/event-stream", seen=None):
    def handler(request):
        if seen is not None:
            seen.append(json.loads(request.content))
        return httpx.Response(status, text=body, headers={"content-type": content_type})
    return httpx.MockTransport(handler)


async def call(body, status=200, content_type="text/event-stream", seen=None):
    async with httpx.AsyncClient(transport=transport(body, status, content_type, seen)) as client:
        return await Provider(config(), client).complete([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_event_stream_content_is_aggregated():
    body = sse(delta(""), delta("正常"), delta("\n第二行"))
    reply = await call(body)
    assert reply["role"] == "assistant"
    assert reply["content"] == "正常\n第二行"
    assert "tool_calls" not in reply


@pytest.mark.asyncio
async def test_reasoning_content_never_becomes_answer():
    body = sse(delta("", reasoning_content="内部推理"), delta("结论"))
    reply = await call(body)
    assert reply["content"] == "结论"


@pytest.mark.asyncio
async def test_event_stream_tool_calls_are_merged_by_index():
    first = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "get_device", "arguments": ""}}]}}]}
    second = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "{\"id\":"}},
        {"index": 1, "id": "call_b", "type": "function", "function": {"name": "list_alarms", "arguments": "{}"}}]}}]}
    third = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": " 2}"}}]}}]}
    reply = await call(sse(first, second, third))
    assert reply["content"] is None
    assert [c["id"] for c in reply["tool_calls"]] == ["call_a", "call_b"]
    assert reply["tool_calls"][0]["function"] == {"name": "get_device", "arguments": '{"id": 2}'}
    assert reply["tool_calls"][1]["function"]["name"] == "list_alarms"


@pytest.mark.asyncio
async def test_plain_json_response_still_supported():
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "正常"}}]})
    reply = await call(body, content_type="application/json")
    assert reply["content"] == "正常"


@pytest.mark.asyncio
async def test_keepalive_only_stream_reports_missing_content():
    with pytest.raises(ModelError) as error:
        await call(": keepalive\n\n")
    assert "未返回内容" in str(error.value)


@pytest.mark.asyncio
async def test_request_uses_streaming_protocol():
    seen = []
    await call(sse(delta("ok")), seen=seen)
    assert seen[0]["stream"] is True
    assert seen[0]["model"] == "deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_auth_failure_is_reported_without_leaking_key():
    with pytest.raises(ModelError) as error:
        await call("{}", status=401, content_type="application/json")
    message = str(error.value)
    assert "模型认证失败" in message
    assert "private-model-key" not in message


def test_merge_tool_calls_handles_missing_index():
    target = []
    merge_tool_calls(target, [{"id": "x", "function": {"name": "a", "arguments": "{}"}}])
    merge_tool_calls(target, [{"id": "y", "function": {"name": "b", "arguments": "{}"}}])
    assert [c["id"] for c in target] == ["x", "y"]


def test_parse_event_stream_ignores_malformed_frames():
    content, calls = parse_event_stream("data: {not json}\n\nevent: ping\n\ndata: [DONE]\n\n")
    assert content is None and calls is None
