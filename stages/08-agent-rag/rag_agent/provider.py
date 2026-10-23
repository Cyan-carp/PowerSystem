import asyncio
import json
import httpx


class ModelError(Exception):
    pass


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
    """Rebuild an assistant message from a completed SSE body.

    The gateway in use ignores stream=false and answers with an event stream,
    so the non-streaming caller has to aggregate delta chunks itself.
    Comment lines (": keepalive") carry no content and are skipped.
    """
    content = []
    calls = []
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
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            continue
        choice = choices[0]
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
    return ("".join(content) or None), (calls or None)


def message_from_json(payload):
    choices = payload["choices"]
    if not isinstance(choices, list) or not choices:
        raise TypeError()
    message = choices[0]["message"]
    if not isinstance(message, dict):
        raise TypeError()
    calls = message.get("tool_calls")
    return message.get("content"), (calls if isinstance(calls, list) and calls else None)


class Provider:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def complete(self, messages, schemas=None):
        cfg = self.config
        # stream=true is required: the gateway answers non-streaming requests with
        # an empty keepalive event stream, so the payload is aggregated locally.
        body = {"model": cfg.model, "messages": messages, "max_tokens": cfg.max_tokens, "stream": True}
        if schemas:
            body.update(tools=schemas, tool_choice="auto")
        body = json.loads(cfg.redact(json.dumps(body, ensure_ascii=False)))
        try:
            async with asyncio.timeout(cfg.model_timeout):
                response = await self.client.post(cfg.llm_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + cfg.llm_key}, json=body, timeout=cfg.model_timeout)
                if response.status_code != 200:
                    reasons = {401: "模型认证失败", 402: "模型余额不足", 403: "模型访问被拒绝", 429: "模型服务限流"}
                    raise ModelError(reasons.get(response.status_code, "模型服务异常") + f"（HTTP {response.status_code}）")
                if "text/event-stream" in response.headers.get("content-type", "").lower():
                    content, calls = parse_event_stream(response.text)
                else:
                    content, calls = message_from_json(response.json())
                if (not isinstance(content, str) or not content.strip()) and not calls:
                    raise ModelError("模型服务未返回内容")
                return {"role": "assistant", "content": content,
                        **({"tool_calls": calls} if calls else {})}
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("模型请求超时") from None
        except httpx.HTTPError:
            raise ModelError("无法连接模型服务") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型响应格式异常") from None
