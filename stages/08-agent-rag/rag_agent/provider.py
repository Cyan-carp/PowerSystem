import asyncio
import json
import httpx


MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
TRUNCATED_FINISH_REASONS = {"length": "模型输出因长度上限被截断",
                            "content_filter": "模型输出被内容过滤拦截"}


class ModelError(Exception):
    def __init__(self, message, category="model_error"):
        super().__init__(message)
        self.category = category


def clean_messages(messages):
    """Only forward protocol fields; usage/finish_reason are local metadata."""
    cleaned = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        cleaned.append({key: message[key] for key in MESSAGE_KEYS if key in message})
    return cleaned


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


def validate_tool_calls(calls):
    """Reject partial or non-object tool arguments before anything executes."""
    if not isinstance(calls, list) or not calls:
        return None
    normalized = []
    for call in calls:
        if not isinstance(call, dict):
            raise ModelError("模型工具调用格式非法", "invalid_tool_arguments")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id.strip() or not isinstance(function, dict):
            raise ModelError("模型工具调用缺少标识或函数", "invalid_tool_arguments")
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ModelError("模型工具调用缺少函数名", "invalid_tool_arguments")
        arguments = function.get("arguments")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str) or not arguments.strip():
            raise ModelError("模型工具调用参数为空", "invalid_tool_arguments")
        try:
            parsed = json.loads(arguments, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
        except ValueError:
            raise ModelError("模型工具调用参数不完整或非法", "invalid_tool_arguments") from None
        if not isinstance(parsed, dict):
            raise ModelError("模型工具调用参数必须是完整JSON对象", "invalid_tool_arguments")
        if any(c['id'] == call_id for c in normalized):
            raise ModelError('模型工具调用标识重复', 'invalid_tool_arguments')
        normalized.append({"id": call_id, "type": "function",
                           "function": {"name": name, "arguments": arguments}})
    return normalized


def _stream_detail(raw):
    content, calls = [], []
    usage, finish_reason = None, None
    frames, done = 0, False
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            break
        try:
            data = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        frames += 1
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
        if isinstance(choice.get("finish_reason"), str):
            finish_reason = choice["finish_reason"]
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            # A gateway may emit a full message instead of a delta.
            delta = choice.get("message")
        if not isinstance(delta, dict):
            continue
        piece = delta.get("content")
        if isinstance(piece, str):
            content.append(piece)
        if isinstance(delta.get("tool_calls"), list):
            merge_tool_calls(calls, delta["tool_calls"])
    return {"content": "".join(content) or None, "tool_calls": calls or None,
            "usage": usage, "finish_reason": finish_reason, "frames": frames, "done": done}


def parse_event_stream(raw):
    """Rebuild an assistant message from a completed SSE body.

    The gateway in use ignores stream=false and answers with an event stream,
    so the non-streaming caller has to aggregate delta chunks itself.
    Comment lines (": keepalive") carry no content and are skipped.
    """
    detail = _stream_detail(raw)
    return detail["content"], detail["tool_calls"]


def parse_event_stream_detail(raw):
    return _stream_detail(raw)


def _json_detail(payload):
    choices = payload["choices"]
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise TypeError()
    choice = choices[0]
    message = choice["message"]
    if not isinstance(message, dict):
        raise TypeError()
    calls = message.get("tool_calls")
    finish = choice.get("finish_reason")
    return {"content": message.get("content"),
            "tool_calls": calls if isinstance(calls, list) and calls else None,
            "usage": payload.get("usage") if isinstance(payload.get("usage"), dict) else None,
            "finish_reason": finish if isinstance(finish, str) else None,
            "frames": 1, "done": True}


def message_from_json(payload):
    detail = _json_detail(payload)
    return detail["content"], detail["tool_calls"]


def parse_json_detail(payload):
    return _json_detail(payload)


class Provider:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def complete(self, messages, schemas=None):
        cfg = self.config
        # stream=true is required: the gateway answers non-streaming requests with
        # an empty keepalive event stream, so the payload is aggregated locally.
        body = {"model": cfg.model, "messages": clean_messages(messages),
                "max_tokens": cfg.max_tokens, "stream": True}
        if cfg.model_thinking is not None:
            body['thinking']={'type':cfg.model_thinking}
        if schemas:
            body.update(tools=schemas, tool_choice="auto")
        body = json.loads(cfg.redact(json.dumps(body, ensure_ascii=False)))
        streamed = False
        try:
            async with asyncio.timeout(cfg.model_timeout):
                response = await self.client.post(cfg.llm_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + cfg.llm_key}, json=body, timeout=cfg.model_timeout)
                if response.status_code != 200:
                    reasons = {401: "模型认证失败", 402: "模型余额不足", 403: "模型访问被拒绝", 429: "模型服务限流"}
                    raise ModelError(reasons.get(response.status_code, "模型服务异常") + f"（HTTP {response.status_code}）",
                                     "http_" + str(response.status_code))
                if "text/event-stream" in response.headers.get("content-type", "").lower():
                    streamed = True
                    detail = _stream_detail(response.text)
                else:
                    try:
                        detail = _json_detail(response.json())
                    except (ValueError, KeyError, TypeError, IndexError):
                        # Some gateways mislabel an SSE body as plain JSON.
                        if not response.text.lstrip().startswith(("data:", ":")):
                            raise
                        streamed = True
                        detail = _stream_detail(response.text)
                if detail["finish_reason"] in TRUNCATED_FINISH_REASONS:
                    reason = detail["finish_reason"]
                    raise ModelError(TRUNCATED_FINISH_REASONS[reason], "finish_reason_" + reason)
                content, calls = detail["content"], detail["tool_calls"]
                if not isinstance(content, str) or not content.strip():
                    content = None
                if content is None and not calls:
                    raise ModelError("模型服务未返回内容" + ("（仅心跳或空响应）" if not detail["frames"] else ""),
                                     "empty_response")
                if streamed and not detail["done"]:
                    # A cut-off stream must not be used as a finished answer.
                    raise ModelError("模型响应不完整：缺少结束事件", "incomplete_stream")
                if calls:
                    calls = validate_tool_calls(calls)
                message = {"role": "assistant", "content": content,
                           "finish_reason": detail["finish_reason"], "usage": detail["usage"]}
                if calls:
                    message["tool_calls"] = calls
                return message
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("模型请求超时", "timeout") from None
        except httpx.HTTPError:
            raise ModelError("无法连接模型服务", "connection") from None
        except ModelError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型响应格式异常", "malformed_response") from None
