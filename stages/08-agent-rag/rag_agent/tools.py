import asyncio
import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Query(Args):
    query: str = Field(min_length=1)


class KnowledgeQuery(Query):
    limit: int = Field(default=5, ge=1, le=20)


class WebQuery(Query):
    count: int = Field(default=5, ge=1, le=10)
    freshness: Literal["noLimit", "oneDay", "oneWeek", "oneMonth", "oneYear"] = "noLimit"


class Page(Args):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=100)


class Devices(Page):
    keyword: str | None = None
    group_name: str | None = None


class Device(Args):
    device_id: int = Field(gt=0)


class Telemetry(Device):
    mode: Literal["latest", "history"] = "latest"
    metric: Literal["voltage", "current", "temperature", "power"] | None = None
    start: str | None = None
    end: str | None = None


class Alarms(Page):
    device_id: int | None = Field(default=None, gt=0)
    status: Literal["unhandled", "acked", "recovered"] | None = None
    level: Literal["urgent", "major", "minor"] | None = None
    start: str | None = None
    end: str | None = None


class Statistics(Args):
    group_by: Literal["device", "metric", "level", "status"] = "device"
    device_id: int | None = Field(default=None, gt=0)
    status: Literal["unhandled", "acked", "recovered"] | None = None
    start: str | None = None
    end: str | None = None


class Alarm(Args):
    alarm_id: int = Field(gt=0)


MODELS = {"search_knowledge": KnowledgeQuery, "web_search": WebQuery, "list_devices": Devices, "get_device": Device,
    "get_telemetry": Telemetry, "get_prediction": Device, "list_alarms": Alarms,
    "get_alarm_statistics": Statistics, "get_alarm_detail": Alarm, "get_dashboard_summary": Args}
DESCRIPTIONS = {
    "search_knowledge": "语义向量检索内部项目资料，返回相关章节与来源。",
    "web_search": "博查联网搜索普通问题、最新信息和技术资料，返回网页摘要、发布时间与URL。",
    "list_devices": "查询平台设备台账，按ID升序分页；第二台指ID升序第二项。名称或编码可以用keyword检索。",
    "get_device": "读取指定设备台账及编码。",
    "get_telemetry": "查询设备最新遥测或指标历史。最新值含ts_ms采样时间、received_at接收时间和status状态：0停机，1运行，2故障。数据可能陈旧，应结合时间理解；history需要metric及带时区start/end。",
    "get_prediction": "查询设备预测概率、阈值、风险、来源及stale过期标志。",
    "list_alarms": "分页读取设备告警。unhandled未确认，acked已确认未恢复，recovered已恢复；可按设备、状态、级别及触发时间筛选。",
    "get_alarm_statistics": "数据库聚合全量告警事件。默认近30天按触发时间统计含已恢复与未恢复，返回时间范围、总数、分组及完整性；适用于排名与统计。",
    "get_alarm_detail": "读取单条告警详情。",
    "get_dashboard_summary": "读取平台设备数、在线数、告警数及功率汇总。"
}


def business_route(name, args):
    args = dict(args)
    if name == "list_devices": return "/api/v1/devices", args
    if name == "list_alarms": return "/api/v1/alarms", args
    if name == "get_alarm_statistics": return "/api/v1/alarms/statistics", args
    if name == "get_alarm_detail": return f"/api/v1/alarms/{args['alarm_id']}", {}
    if name == "get_dashboard_summary": return "/api/v1/dashboard/summary", {}
    device_id = args.pop("device_id")
    if name == "get_device": return f"/api/v1/devices/{device_id}", {}
    if name == "get_prediction": return f"/api/v1/devices/{device_id}/prediction", {}
    if args.pop("mode") == "latest": return f"/api/v1/devices/{device_id}/telemetry/latest", {}
    if not all(args.get(k) for k in ("metric", "start", "end")):
        raise ValueError("历史遥测需要metric/start/end")
    return f"/api/v1/devices/{device_id}/telemetry", args


def public_link(url):
    try:
        p = urlsplit(url)
        return p.scheme in ("https", "http") and bool(p.hostname) and not p.username and not p.password
    except (ValueError, TypeError):
        return False


class Tools:
    schemas = [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
               "parameters": model.model_json_schema()}} for name, model in MODELS.items()]

    def __init__(self, config, client, knowledge):
        self.config, self.client, self.knowledge = config, client, knowledge

    async def execute(self, name, arguments, jwt, result):
        stamp = datetime.now(timezone.utc).isoformat()
        trace = {"name": name, "arguments": {}, "status": "error", "collected_at": stamp}
        result["tool_calls"].append(trace)
        try:
            args = MODELS[name].model_validate(json.loads(arguments)).model_dump(exclude_none=True)
            args = json.loads(self.config.redact(json.dumps(args, ensure_ascii=False), (jwt,)))
            trace["arguments"] = args
            async with asyncio.timeout(self.config.tool_timeout):
                if name == "search_knowledge":
                    rows = await self.knowledge.search(**args)
                    data = {"results": rows}
                    for row in rows:
                        result["sources"].append({"id": row["id"], "kind": "document", "title": row["document"],
                            "chapter": row["heading"], "url": f"/api/v1/agent/rag/sources/{row['id']}",
                            "version": row["version"], "page": row.get("page"), "collected_at": stamp})
                elif name == "web_search":
                    if not self.config.search_key:
                        raise ValueError("博查未配置密钥")
                    reply = await self.client.post("https://api.bocha.cn/v1/web-search",
                        headers={"Authorization": "Bearer " + self.config.search_key},
                        json={**args, "summary": True}, timeout=self.config.tool_timeout)
                    if reply.status_code != 200:
                        raise ValueError(f"博查服务HTTP {reply.status_code}")
                    payload = reply.json()
                    if str(payload.get("code")) != "200":
                        raise ValueError("博查服务返回错误")
                    rows = []
                    for item in payload.get("data", {}).get("webPages", {}).get("value", []):
                        if not public_link(item.get("url")): continue
                        row = {"title": item.get("name", "网页"), "url": item["url"],
                            "summary": item.get("summary") or item.get("snippet", ""), "published_at": item.get("datePublished")}
                        rows.append(row)
                        result["sources"].append({"id": "W" + str(len(result["sources"]) + 1), "kind": "web", **row, "collected_at": stamp})
                    data = {"results": rows}
                else:
                    path, params = business_route(name, args)
                    reply = await self.client.get(self.config.backend_url + path, params=params,
                        headers={"Authorization": "Bearer " + jwt}, timeout=self.config.tool_timeout)
                    if reply.status_code != 200:
                        raise ValueError(f"平台接口HTTP {reply.status_code}")
                    envelope = reply.json()
                    if envelope.get("code") != 0: raise ValueError("平台接口返回错误")
                    data = envelope["data"]
                    if name == "list_devices": data = {**data, "order": "id ASC"}
                    result["sources"].append({"id": "B" + str(len(result["sources"]) + 1), "kind": "business",
                        "title": DESCRIPTIONS[name].split("。")[0], "url": path, "collected_at": stamp})
            encoded = self.config.redact(json.dumps(data, ensure_ascii=False), (jwt,))
            if len(encoded) > self.config.tool_chars:
                data = {"truncated": True, "preview": encoded[:self.config.tool_chars]}
                result["warnings"].append(name + "返回数据较大，已截取用于模型上下文。")
            else:
                data = json.loads(encoded)
            trace.update(status="ok", result=data)
            return data
        except asyncio.CancelledError:
            trace["status"] = "cancelled"
            raise
        except Exception as exc:
            # Exception strings can contain private URLs; only validation messages are exposed.
            reason = self.config.redact(str(exc), (jwt,)) if isinstance(exc, ValueError) and not hasattr(exc, "errors") else type(exc).__name__
            trace["error"] = reason
            result["warnings"].append(name + "失败：" + reason)
            return {"error": reason}
