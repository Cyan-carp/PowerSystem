import asyncio
import json
import httpx


class ModelError(Exception):
    pass


class Provider:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def complete(self, messages, schemas=None):
        cfg = self.config
        body = {"model": cfg.model, "messages": messages, "max_tokens": cfg.max_tokens, "stream": False}
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
                msg = response.json()["choices"][0]["message"]
                return {"role": "assistant", "content": msg.get("content"),
                        **({"tool_calls": msg["tool_calls"]} if msg.get("tool_calls") else {})}
        except (TimeoutError, httpx.TimeoutException):
            raise ModelError("模型请求超时") from None
        except httpx.HTTPError:
            raise ModelError("无法连接模型服务") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("模型响应格式异常") from None
