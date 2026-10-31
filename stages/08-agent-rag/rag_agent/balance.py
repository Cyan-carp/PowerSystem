"""只读余额查询：只查本服务自己持有的主助手 Key，不接收调用方传入的地址或密钥。

OpenAI 兼容协议本身没有额度查询端点，余额是供应商私有的：
DeepSeek 提供 `GET /user/balance`，其余兼容供应商没有统一接口。
因此地址不是官方域名时如实返回 `unsupported`，不去猜别家的计费接口——
猜错会把「查不到」伪装成「没额度」，比不显示更糟。
"""

from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

OFFICIAL_HOST = "api.deepseek.com"
MAX_BYTES = 64 * 1024
MAX_ENTRIES = 8


def balance_endpoint(base_url: str) -> str:
    """从已配置的 base_url 推出余额端点；只认官方域名，路径固定。

    官方 base 可能带 `/v1` 后缀，余额端点在站点根下，所以要按 host 重建而不是简单拼接。
    """
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or parsed.hostname != OFFICIAL_HOST:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}/user/balance"


def _amount(value) -> str:
    """官方返回的金额是字符串（如 "110.00"）。保持原样，不转浮点，避免精度与四舍五入失真。"""
    return value if isinstance(value, str) else ""


async def fetch_balance(client, *, channel, label, base_url, model, api_key, timeout=8.0) -> dict:
    result = {
        "channel": channel,
        "label": label,
        "model": model,
        "base_url": base_url,
        "supported": False,
        "is_available": None,
        "balances": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "error": "",
    }

    endpoint = balance_endpoint(base_url)
    if not endpoint:
        result["error"] = "unsupported_provider"
        return result
    result["supported"] = True

    if not api_key:
        result["error"] = "missing_key"
        return result

    try:
        response = await client.get(
            endpoint,
            headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
            timeout=timeout,
        )
    except httpx.TimeoutException:
        result["error"] = "timeout"
        return result
    except httpx.HTTPError:
        result["error"] = "unreachable"
        return result

    if response.status_code == 401:
        result["error"] = "auth_failed"
        return result
    if response.status_code != 200:
        result["error"] = f"http_{response.status_code}"
        return result

    raw = response.content
    if len(raw) > MAX_BYTES:
        result["error"] = "response_too_large"
        return result

    try:
        payload = response.json()
    except ValueError:
        result["error"] = "invalid_response"
        return result
    if not isinstance(payload, dict):
        result["error"] = "invalid_response"
        return result

    infos = payload.get("balance_infos")
    if isinstance(infos, list):
        for item in infos[:MAX_ENTRIES]:
            if not isinstance(item, dict):
                continue
            result["balances"].append(
                {
                    "currency": str(item.get("currency") or ""),
                    "total_balance": _amount(item.get("total_balance")),
                    "granted_balance": _amount(item.get("granted_balance")),
                    "topped_up_balance": _amount(item.get("topped_up_balance")),
                }
            )

    # 官方语义：is_available=false 表示余额不足，调用会被拒绝（402）。
    result["is_available"] = payload.get("is_available") is True
    return result
