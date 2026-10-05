"""Configured search API only. No arbitrary browsing or user content export."""
import asyncio
import ipaddress
import json
from urllib.parse import urlsplit
import httpx

# Queries are built from a public technical vocabulary, never the raw user question.
PUBLIC_TERMS = {"逆变器": "逆变器", "光伏": "光伏", "风机": "风力发电机", "结冰": "结冰", "覆冰": "覆冰",
    "绝缘": "绝缘", "接地": "接地", "电弧": "电弧", "过热": "过热", "通信": "通信",
    "mqtt": "MQTT", "emqx": "EMQX", "redis": "Redis", "postgresql": "PostgreSQL",
    "tdengine": "TDengine", "nginx": "Nginx", "证书": "TLS证书", "告警": "告警",
    "温度": "温度", "电压": "电压", "电流": "电流", "效率": "效率", "故障": "故障"}


def safe_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return False
        host = parsed.hostname.lower()
        if host == "localhost" or host.endswith((".local", ".internal")) or "." not in host:
            return False
        try:
            if not ipaddress.ip_address(host).is_global:
                return False
        except ValueError:
            pass
        if parsed.query or parsed.fragment:
            # Search citations must not carry tokens, tracking or private request values.
            return False
        return True
    except (ValueError, TypeError):
        return False


def public_query(message):
    if any(term in message.lower() for term in ("本项目", "我的设备", "当前设备", "账号", "密码", "密钥", "服务器地址", "页面", "按钮", "现在", "实时", "这台")):
        return ""
    terms = list(dict.fromkeys(value for key, value in PUBLIC_TERMS.items() if key in message.lower()))
    return " ".join(terms[:6]) + " 官方文档 排查" if len(terms) >= 2 else ""


BOCHA_URL = "https://api.bocha.cn/v1/web-search"
SEARCH_TIMEOUT = 5
STATUS_LABELS = {
    "not_configured": "未配置搜索接口或密钥", "auth_failed": "搜索密钥认证失败",
    "quota_exhausted": "搜索账户余额不足", "rate_limited": "搜索请求限流",
    "timeout": "联网检索超时", "no_results": "没有可用的安全来源",
    "invalid_response": "搜索响应异常", "unavailable": "搜索服务不可用",
    "cancelled": "联网检索已取消",
}


class Search:
    def __init__(self, config, client):
        self.config, self.client = config, client

    @property
    def secrets(self):
        return (self.config.api_key, self.config.service_token, self.config.monitor_token, self.config.search_key)

    @staticmethod
    def error(status, payload):
        code = str(payload.get("code", ""))
        if (status == 403 or code == "403") and payload.get("message", payload.get("msg")) == "You do not have enough money":
            return "quota_exhausted", []
        if status in (401, 403) or code in ("401", "403"):
            return "auth_failed", []
        if status == 429 or code == "429":
            return "rate_limited", []
        return ("invalid_response" if status == 200 else "unavailable"), []

    async def run(self, query):
        bocha = self.config.search_provider == "bocha"
        url = BOCHA_URL if bocha else self.config.search_url
        if not url or not self.config.search_key:
            return "not_configured", []
        body = {"query": query, "count": 5, "summary": True, "freshness": "noLimit"} if bocha else {"query": query, "max_results": 5}
        try:
            # Includes headers, all streamed chunks and parsing; no redirect or retry.
            async with asyncio.timeout(SEARCH_TIMEOUT):
                async with self.client.stream("POST", url, headers={"Authorization": "Bearer " + self.config.search_key},
                        json=body, timeout=SEARCH_TIMEOUT, follow_redirects=False) as response:
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        if len(raw) + len(part) > 65536:
                            return "invalid_response", []
                        raw.extend(part)
                    if not bocha and response.status_code != 200:
                        return "auth_failed" if response.status_code in (401, 403) else "unavailable", []
                    if bocha:
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            return self.error(response.status_code, {}) if response.status_code != 200 else ("invalid_response", [])
                        if not isinstance(payload, dict):
                            return "invalid_response", []
                        if response.status_code != 200 or str(payload.get("code", "")) != "200":
                            return self.error(response.status_code, payload)
                        results = payload["data"]["webPages"]["value"]
                    else:
                        results = json.loads(raw)["results"]
                if not isinstance(results, list):
                    return "invalid_response", []
                clean, seen = [], set()
                for result in (results if bocha else results[:5]):
                    if not isinstance(result, dict) or not safe_url(result.get("url", "")):
                        continue
                    link = result["url"]
                    if bocha and link in seen:
                        continue
                    if any(secret and secret in link for secret in self.secrets):
                        continue
                    title = result.get("name" if bocha else "title")
                    summary = result.get("summary")
                    if bocha and (not isinstance(summary, str) or not summary.strip()):
                        summary = result.get("snippet")
                    elif not bocha:
                        summary = result.get("summary", result.get("content"))
                    if not isinstance(title, str) or not isinstance(summary, str) or not title.strip() or not summary.strip():
                        continue
                    published = result.get("datePublished" if bocha else "published_at")
                    row = {"title": title[:200], "url": link, "summary": summary[:2000],
                           "published_at": published[:128] if isinstance(published, str) and published.strip() else None}
                    for field in ("title", "summary", "published_at"):
                        if isinstance(row[field], str):
                            for secret in self.secrets:
                                if secret:
                                    row[field] = row[field].replace(secret, "[REDACTED]")
                    clean.append(row); seen.add(link)
                    if len(clean) == 5:
                        break
                return ("hit" if clean else "no_results"), clean
        except (TimeoutError, httpx.TimeoutException):
            return "timeout", []
        except httpx.HTTPError:
            return "unavailable", []
        except (ValueError, KeyError, TypeError):
            return "invalid_response", []
