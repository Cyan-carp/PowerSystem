import json

import httpx
import pytest

from security_agent.provider import merge_tool_calls, message_from_json, parse_event_stream, read_message


def sse(*chunks):
    lines = [": keepalive", ""]
    for chunk in chunks:
        lines.append("data: " + json.dumps(chunk, ensure_ascii=False))
    lines.append("data: [DONE]")
    return "\n\n".join(lines) + "\n\n"


def delta(content=None, **extra):
    return {"choices": [{"index": 0, "delta": {"content": content, **extra}, "finish_reason": None}]}


def test_event_stream_content_is_aggregated():
    content, calls, usage = parse_event_stream(sse(delta('{"decision":'), delta('"allow"}')))
    assert content == '{"decision":"allow"}'
    assert json.loads(content) == {"decision": "allow"}
    assert calls is None and usage == {}


def test_usage_frame_with_empty_choices_is_counted():
    usage_frame = {"choices": [], "usage": {"prompt_tokens": 25, "completion_tokens": 5, "total_tokens": 30,
                                            "cache_read_input_tokens": 12}}
    _, _, usage = parse_event_stream(sse(delta("{}"), usage_frame))
    assert usage["total_tokens"] == 30 and usage["cache_read_input_tokens"] == 12


def test_tool_calls_are_detected_from_stream():
    frame = {"choices": [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}}]}
    content, calls, _ = parse_event_stream(sse(frame))
    assert content is None
    assert calls and calls[0]["id"] == "call_a"


def test_reasoning_content_is_not_part_of_the_opinion():
    content, _, _ = parse_event_stream(sse(delta("", reasoning_content="思考"), delta('{"decision":"deny"}')))
    assert content == '{"decision":"deny"}'


def test_plain_json_body_still_parses():
    content, calls, usage = message_from_json({"choices": [{"message": {"content": '{"decision":"allow"}'}}],
                                               "usage": {"total_tokens": 9, "prompt_tokens": 6}})
    assert json.loads(content) == {"decision": "allow"}
    assert calls is None and usage["total_tokens"] == 9


def test_read_message_routes_by_content_type():
    stream = httpx.Response(200, text=sse(delta("流式")), headers={"content-type": "text/event-stream"})
    plain = httpx.Response(200, json={"choices": [{"message": {"content": "普通"}}]},
                           headers={"content-type": "application/json"})
    assert read_message(stream)[0] == "流式"
    assert read_message(plain)[0] == "普通"


def test_keepalive_only_reply_yields_no_content():
    response = httpx.Response(200, text=": keepalive\n\n", headers={"content-type": "text/event-stream"})
    content, calls, usage = read_message(response)
    assert content is None and calls is None and usage == {}


def test_malformed_frames_are_ignored():
    content, calls, _ = parse_event_stream("data: {not json}\n\nevent: ping\n\ndata: [DONE]\n\n")
    assert content is None and calls is None


def test_merge_handles_out_of_order_arguments():
    target = []
    merge_tool_calls(target, [{"index": 0, "id": "c", "function": {"name": "n", "arguments": "{}"}}])
    merge_tool_calls(target, [{"index": 0, "function": {"arguments": " "}}])
    assert target[0]["function"]["arguments"] == "{} "
