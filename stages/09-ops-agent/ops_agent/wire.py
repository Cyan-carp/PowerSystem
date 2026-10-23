"""Model wire compatibility for the ops agent.

The gateway behind ``OPS_LLM_URL`` answers every completion request with an
event stream, even when ``stream`` is false: the non-streaming reply is a bare
``: keepalive`` frame with no ``data:`` payload. Requests therefore go out with
``stream: true`` and the chunks are aggregated here. JSON bodies are still
accepted, so a gateway that restores non-streaming replies keeps working.

Keep the parsing rules in sync with ``stages/08-agent-rag/rag_agent/provider.py``
and ``stages/10-security-agent/security_agent/provider.py``.
"""
import json


def merge_tool_calls(target, deltas):
    """Merge OpenAI-style incremental tool_call deltas into a full list."""
    for delta in deltas:
        if not isinstance(delta, dict):
            continue
        index = delta.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            index = len(target)
        while len(target) <= index:
            target.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
        slot = target[index]
        if isinstance(delta.get("id"), str) and delta["id"] and not slot["id"]:
            slot["id"] = delta["id"]
        function = delta.get("function")
        if not isinstance(function, dict):
            continue
        if isinstance(function.get("name"), str) and function["name"] and not slot["function"]["name"]:
            slot["function"]["name"] = function["name"]
        if isinstance(function.get("arguments"), str):
            slot["function"]["arguments"] += function["arguments"]
    return target


def parse_event_stream(raw):
    """Rebuild (content, tool_calls, usage) from a completed SSE body.

    Comment lines (``: keepalive``) and empty payloads carry no content and are
    skipped. Frames whose ``choices`` list is empty still may carry ``usage``.
    """
    content = []
    calls = []
    usage = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("usage"), dict) and data["usage"]:
            usage = data["usage"]
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        delta = choices[0].get("delta")
        if not isinstance(delta, dict):
            # A gateway may emit a full message instead of a delta.
            delta = choices[0].get("message")
        if not isinstance(delta, dict):
            continue
        piece = delta.get("content")
        if isinstance(piece, str):
            content.append(piece)
        if isinstance(delta.get("tool_calls"), list):
            merge_tool_calls(calls, delta["tool_calls"])
    return ("".join(content) or None), (calls or None), usage


def message_from_json(payload):
    """Read (content, tool_calls, usage) from a non-streaming completion body."""
    choices = payload["choices"]
    if not isinstance(choices, list) or not choices:
        raise TypeError()
    message = choices[0]["message"]
    if not isinstance(message, dict):
        raise TypeError()
    calls = message.get("tool_calls")
    usage = payload.get("usage")
    return message.get("content"), (calls if isinstance(calls, list) and calls else None), (usage if isinstance(usage, dict) else {})


def read_message(response):
    """Return the assistant message, accepting either wire format."""
    if "text/event-stream" in response.headers.get("content-type", "").lower():
        content, calls, _ = parse_event_stream(response.text)
    else:
        content, calls, _ = message_from_json(response.json())
    if (not isinstance(content, str) or not content.strip()) and not calls:
        raise ValueError("model_invalid_output")
    return {"role": "assistant", "content": content, **({"tool_calls": calls} if calls else {})}
