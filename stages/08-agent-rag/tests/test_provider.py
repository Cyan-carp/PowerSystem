import json
import httpx
import pytest
from rag_agent.config import Config
from rag_agent.provider import ModelError, Provider, merge_tool_calls, parse_event_stream


def config():
    return Config("deepseek-v4.1-flash", "https://model.example/v1", "private-model-key", "s" * 32,
                  "postgresql://private", "redis://private")


@pytest.mark.asyncio
async def test_explicit_disabled_thinking_is_sent_without_changing_default():
    seen=[];cfg=config();cfg.model_thinking='disabled'
    body=json.dumps({'choices':[{'message':{'content':'ok'},'finish_reason':'stop'}]})
    async with httpx.AsyncClient(transport=transport(body,content_type='application/json',seen=seen)) as client:
        await Provider(cfg,client).complete([{'role':'user','content':'synthetic'}])
    assert seen[0]['thinking']=={'type':'disabled'}
    assert cfg.llm_key not in json.dumps(seen)


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


def raw_sse(*chunks, done=True):
    lines = [": keepalive", ""]
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False))
    if done:
        lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def finish_frame(reason):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def tool_frame(call_id, name, arguments, index=0):
    return {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": index, "id": call_id, "type": "function",
         "function": {"name": name, "arguments": arguments}}]}}]}


@pytest.mark.asyncio
async def test_length_truncation_is_rejected_not_returned_as_answer():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(delta("半截"), finish_frame("length")))
    assert error.value.category == "finish_reason_length"
    assert "截断" in str(error.value)


@pytest.mark.asyncio
async def test_content_filter_is_rejected():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(delta("x"), finish_frame("content_filter")))
    assert error.value.category == "finish_reason_content_filter"


@pytest.mark.asyncio
async def test_stream_without_end_event_is_rejected():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(delta("看起来正常"), done=False))
    assert error.value.category == "incomplete_stream"
    assert "结束事件" in str(error.value)


@pytest.mark.asyncio
async def test_usage_chunk_is_metadata_not_content():
    usage = {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}
    reply = await call(raw_sse(delta("答案"), {"choices": [], "usage": usage}))
    assert reply["content"] == "答案"
    assert reply["usage"] == usage
    assert "total_tokens" not in reply["content"]
    assert reply["finish_reason"] is None


@pytest.mark.asyncio
async def test_incomplete_tool_arguments_are_rejected():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(tool_frame("call_a", "get_device", '{"id":')))
    assert error.value.category == "invalid_tool_arguments"
    assert "不完整" in str(error.value)


@pytest.mark.asyncio
async def test_tool_arguments_must_be_a_json_object():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(tool_frame("call_a", "get_device", '["id", 2]')))
    assert error.value.category == "invalid_tool_arguments"
    assert "JSON对象" in str(error.value)


@pytest.mark.asyncio
async def test_tool_call_without_id_is_rejected():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(tool_frame("", "get_device", '{"id": 2}')))
    assert error.value.category == "invalid_tool_arguments"


@pytest.mark.asyncio
async def test_outgoing_messages_drop_local_metadata():
    seen = []
    metadata = {"role": "assistant", "content": "上一轮", "finish_reason": "stop",
                "usage": {"total_tokens": 3}}
    async with httpx.AsyncClient(transport=transport(raw_sse(delta("ok")), seen=seen)) as client:
        await Provider(config(), client).complete([{"role": "user", "content": "hi"}, metadata])
    forwarded = seen[0]["messages"][1]
    assert forwarded == {"role": "assistant", "content": "上一轮"}


@pytest.mark.asyncio
async def test_server_error_does_not_echo_response_body_or_key():
    body = json.dumps({"error": "boom", "echo": "private-model-key"})
    with pytest.raises(ModelError) as error:
        await call(body, status=500, content_type="application/json")
    assert "private-model-key" not in str(error.value)
    assert "boom" not in str(error.value)


@pytest.mark.asyncio
async def test_plain_json_keeps_usage_and_finish_reason():
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop"}],
                       "usage": {"total_tokens": 7}})
    reply = await call(body, content_type="application/json")
    assert reply["usage"]["total_tokens"] == 7 and reply["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_plain_json_truncation_is_rejected():
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "半截"},
                                    "finish_reason": "length"}]})
    with pytest.raises(ModelError) as error:
        await call(body, content_type="application/json")
    assert error.value.category == "finish_reason_length"


@pytest.mark.asyncio
async def test_json_tool_arguments_object_is_normalized_to_string():
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": "call_a", "type": "function",
                        "function": {"name": "get_device", "arguments": {"id": 2}}}]},
        "finish_reason": "tool_calls"}]})
    reply = await call(body, content_type="application/json")
    assert reply["tool_calls"][0]["function"]["arguments"] == '{"id": 2}'


@pytest.mark.asyncio
async def test_rate_limit_is_reported_as_model_limit():
    with pytest.raises(ModelError) as error:
        await call("{}", status=429, content_type="application/json")
    assert "限流" in str(error.value) and error.value.category == "http_429"


@pytest.mark.asyncio
async def test_timeout_is_reported_without_transport_details():
    def handler(request):
        raise httpx.ReadTimeout("private-model-key timed out", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ModelError) as error:
            await Provider(config(), client).complete([{"role": "user", "content": "hi"}])
    assert error.value.category == "timeout" and "private-model-key" not in str(error.value)


@pytest.mark.asyncio
async def test_non_json_body_is_rejected_as_malformed():
    with pytest.raises(ModelError) as error:
        await call("<html>gateway</html>", content_type="application/json")
    assert error.value.category == "malformed_response"


@pytest.mark.asyncio
async def test_json_payload_without_choices_is_rejected():
    with pytest.raises(ModelError) as error:
        await call(json.dumps({"usage": {"total_tokens": 1}}), content_type="application/json")
    assert error.value.category == "malformed_response"


@pytest.mark.asyncio
async def test_nonfinite_tool_arguments_never_execute():
    with pytest.raises(ModelError) as error:
        await call(raw_sse(tool_frame('a','get_device','{"id": NaN}')))
    assert error.value.category=='invalid_tool_arguments'


@pytest.mark.asyncio
async def test_duplicate_tool_ids_are_rejected():
    with pytest.raises(ModelError):
        await call(raw_sse(tool_frame('same','get_device','{}',0),tool_frame('same','get_device','{}',1)))
