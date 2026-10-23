import json

import httpx
import pytest

from ops_agent.wire import merge_tool_calls, message_from_json, parse_event_stream, read_message


def sse(*chunks):
    lines = [": keepalive", ""]
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def delta(content=None, **extra):
    return {"choices": [{"index": 0, "delta": {"content": content, **extra}, "finish_reason": None}]}


def test_event_stream_content_is_aggregated():
    content, calls, usage = parse_event_stream(sse(delta(""), delta("逆变器"), delta("正常")))
    assert content == "逆变器正常"
    assert calls is None
    assert usage == {}


def test_reasoning_content_never_becomes_answer():
    content, _, _ = parse_event_stream(sse(delta("", reasoning_content="内部推理"), delta("结论")))
    assert content == "结论"


def test_tool_calls_are_merged_by_index():
    first = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"arguments": "", "name": "assets__list"}}]}}]}
    second = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"limit": '}},
        {"index": 1, "id": "call_b", "type": "function", "function": {"arguments": "", "name": "docker__inspect_container"}}]}}]}
    third = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "2}"}}]}}]}
    content, calls, _ = parse_event_stream(sse(first, second, third))
    assert content is None
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert calls[0]["function"] == {"name": "assets__list", "arguments": '{"limit": 2}'}
    assert calls[1]["function"]["name"] == "docker__inspect_container"


def test_usage_frame_with_empty_choices_is_counted():
    usage_frame = {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}
    content, _, usage = parse_event_stream(sse(delta("好"), usage_frame))
    assert content == "好"
    assert usage["total_tokens"] == 17


def test_plain_json_body_still_parses():
    content, calls, usage = message_from_json({"choices": [{"message": {"role": "assistant", "content": "正常"}}],
                                               "usage": {"total_tokens": 3}})
    assert content == "正常" and calls is None and usage["total_tokens"] == 3


def test_read_message_routes_by_content_type():
    stream = httpx.Response(200, text=sse(delta("流式")), headers={"content-type": "text/event-stream"})
    plain = httpx.Response(200, json={"choices": [{"message": {"content": "普通"}}]},
                           headers={"content-type": "application/json"})
    assert read_message(stream)["content"] == "流式"
    assert read_message(plain)["content"] == "普通"


def test_keepalive_only_reply_is_reported_as_invalid():
    response = httpx.Response(200, text=": keepalive\n\n", headers={"content-type": "text/event-stream"})
    with pytest.raises(ValueError) as error:
        read_message(response)
    assert str(error.value) == "model_invalid_output"


def test_merge_handles_missing_index():
    target = []
    merge_tool_calls(target, [{"id": "x", "function": {"name": "a", "arguments": "{}"}}])
    merge_tool_calls(target, [{"id": "y", "function": {"name": "b", "arguments": "{}"}}])
    assert [c["id"] for c in target] == ["x", "y"]
