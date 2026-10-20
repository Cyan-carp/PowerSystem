import asyncio
import json
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


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

    @model_validator(mode="after")
    def valid_history(self):
        if self.mode == "history":
            try:
                start = datetime.fromisoformat(self.start or "")
                end = datetime.fromisoformat(self.end or "")
                if not self.metric or start.tzinfo is None or end.tzinfo is None or not 0 < (end-start).total_seconds() <= 86400:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError("history requires metric and timezone-aware start/end within 24 hours") from None
        return self


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


class Rules(Page):
    device_id: int | None = Field(default=None, gt=0)


MODELS = {"search_knowledge": KnowledgeQuery, "web_search": WebQuery, "list_devices": Devices, "get_device": Device,
    "get_telemetry": Telemetry, "get_prediction": Device, "list_alarms": Alarms,
    "get_alarm_statistics": Statistics, "get_alarm_detail": Alarm, "get_dashboard_summary": Args,
    "get_alarm_rules": Rules}
DESCRIPTIONS = {
    "search_knowledge": "语义向量检索内部项目资料，返回相关章节与来源。",
    "web_search": "博查联网搜索普通问题、最新信息和技术资料，返回网页摘要、发布时间与URL。",
    "list_devices": "查询平台设备台账，按ID升序分页；第二台指ID升序第二项。名称或编码可以用keyword检索。",
    "get_device": "读取指定设备台账及编码。",
    "get_telemetry": "查询设备最新遥测或指标历史。最新值含ts_ms采样时间、received_at接收时间和status状态：0停机，1运行，2故障。数据可能陈旧，应结合时间理解；history需要metric及RFC3339带时区start/end，end>start且区间最多24小时。",
    "get_prediction": "查询设备预测概率、阈值、风险、来源及stale过期标志。",
    "list_alarms": "分页读取设备告警。unhandled未确认，acked已确认未恢复，recovered已恢复；可按设备、状态、级别及触发时间筛选。",
    "get_alarm_statistics": "数据库聚合全量告警事件。默认近30天按触发时间统计含已恢复与未恢复，返回时间范围、总数、分组及完整性；适用于排名与统计。",
    "get_alarm_detail": "读取单条告警详情。",
    "get_dashboard_summary": "读取平台设备数、在线数、告警数及功率汇总。",
    "get_alarm_rules": "查询设备告警规则（指标、比较运算符、阈值、单位、级别、启用状态、规则版本），用于程序化阈值比较。缺规则或单位不一致时只能说明无法比较，不能自行设定现场阈值。"
}


def business_route(name, args):
    args = dict(args)
    if name == "list_devices": return "/api/v1/devices", args
    if name == "list_alarms": return "/api/v1/alarms", args
    if name == "get_alarm_statistics": return "/api/v1/alarms/statistics", args
    if name == "get_alarm_detail": return f"/api/v1/alarms/{args['alarm_id']}", {}
    if name == "get_dashboard_summary": return "/api/v1/dashboard/summary", {}
    if name == "get_alarm_rules": return "/api/v1/alarm-rules", args
    device_id = args.pop("device_id")
    if name == "get_device": return f"/api/v1/devices/{device_id}", {}
    if name == "get_prediction": return f"/api/v1/devices/{device_id}/prediction", {}
    if args.pop("mode") == "latest": return f"/api/v1/devices/{device_id}/telemetry/latest", {}
    if not all(args.get(k) for k in ("metric", "start", "end")):
        raise ValueError("历史遥测需要metric/start/end")
    return f"/api/v1/devices/{device_id}/telemetry", args
