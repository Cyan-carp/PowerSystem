import asyncio
import json
from datetime import date, datetime, timezone
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


class ScopeOnly(Args):
    scope: Literal["all", "business", "baseline"] = "all"


class ScopeFilter(ScopeOnly):
    station_code: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")


class Devices(Page, ScopeFilter):
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


class Alarms(Page, ScopeFilter):
    device_id: int | None = Field(default=None, gt=0)
    status: Literal["unhandled", "acked", "recovered"] | None = None
    level: Literal["urgent", "major", "minor"] | None = None
    start: str | None = None
    end: str | None = None


class Stations(ScopeOnly):
    pass


class Overview(ScopeFilter):
    pass


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


class Energy(ScopeFilter):
    device_id: int | None = Field(default=None, gt=0)
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def valid_window(self):
        try:
            days = (date.fromisoformat(self.end_date)-date.fromisoformat(self.start_date)).days
            if not 1 <= days <= 31: raise ValueError()
        except ValueError:
            raise ValueError("energy requires 1..31 Beijing calendar days, end exclusive") from None
        return self


MODELS = {"search_knowledge": KnowledgeQuery, "web_search": WebQuery, "list_devices": Devices, "get_device": Device,
    "get_telemetry": Telemetry, "get_prediction": Device, "list_alarms": Alarms,
    "get_alarm_statistics": Statistics, "get_alarm_detail": Alarm, "get_dashboard_summary": Args,
    "list_stations": Stations, "get_dashboard_overview": Overview, "get_alarm_rules": Rules,
    "get_energy_report": Energy}
DESCRIPTIONS = {
    "get_energy_report": "读取energy_daily逐日发电量及汇总kWh。start_date含/end_date不含，北京时间，最多31自然日；近7天默认最近7个已结束自然日，不含今天，并说明窗口。scope/station_code/device_id筛选取交集。序号设备先list_devices按id ASC定位。缺测/无有效段不是零，complete=false不得声称全窗口完成。禁止用功率或24小时遥测代偿。",
    "search_knowledge": "语义向量检索内部项目资料，返回相关章节与来源。",
    "web_search": "博查联网搜索普通问题、最新信息和技术资料，返回网页摘要、发布时间与URL。",
    "list_devices": "查询平台设备台账，按ID升序分页（page/page_size，单页最多100条）；第二台指ID升序第二项。名称或编码可以用keyword检索。scope=all|business|baseline，station_code可限定单个场站。返回含total：只取一页时仅覆盖该页，不能声称已检查全部设备；要全量结论必须按total继续翻页。",
    "get_device": "读取指定设备台账及编码。",
    "get_telemetry": "查询设备最新遥测或指标历史。最新值含ts_ms采样时间、received_at接收时间和status状态：0停机，1运行，2故障。数据可能陈旧，应结合时间理解；history需要metric及RFC3339带时区start/end，end>start且区间最多24小时。",
    "get_prediction": "查询设备预测概率、阈值、风险、来源及stale过期标志。",
    "list_alarms": "分页读取设备告警（page/page_size 按页返回，单页最多100条；按触发时间倒序、同时间按ID倒序）。unhandled未确认，acked已确认未恢复，recovered已恢复；可按设备、状态、级别及触发时间筛选。scope=all|business|baseline，station_code可限定单个场站。返回含total：只取一页时仅覆盖该页，不能声称已覆盖全部告警；要全量结论必须按total继续翻页。",
    "get_alarm_statistics": "数据库聚合全量告警事件。默认近30天按触发时间统计含已恢复与未恢复，返回时间范围、总数、分组及完整性；适用于排名与统计。",
    "get_alarm_detail": "读取单条告警详情。",
    "get_dashboard_summary": "读取平台设备数、在线数、告警数及功率汇总（旧接口，固定全平台口径）。",
    "list_stations": "按共享清单列出场站编码、名称、范围、类型、期望设备数与已登记设备数。scope=all|business|baseline，默认all；名称和范围以清单为准，不要自行猜测或翻译场站。",
    "get_dashboard_overview": "读取当前范围的总览聚合（GET /api/v1/dashboard/overview）：summary、场站对比、computed_at及完整性标记。scope=all|business|baseline，station_code可限定单个场站。partial为真表示未达期望或已登记设备未全部观测；expected/registered/observed分别指清单期望数、台账登记数、当前有观测的设备数；null表示该值当前缺失或不可计算，不等于0，也不代表设备正常。",
    "get_alarm_rules": "查询设备告警规则（指标、比较运算符、阈值、单位、级别、启用状态、规则版本），用于程序化阈值比较。缺规则或单位不一致时只能说明无法比较，不能自行设定现场阈值。"
}


def business_route(name, args):
    args = dict(args)
    if name == "get_energy_report": return "/api/v1/reports/energy", args
    if name == "list_devices": return "/api/v1/devices", args
    if name == "list_alarms": return "/api/v1/alarms", args
    if name == "get_alarm_statistics": return "/api/v1/alarms/statistics", args
    if name == "get_alarm_detail": return f"/api/v1/alarms/{args['alarm_id']}", {}
    if name == "get_dashboard_summary": return "/api/v1/dashboard/summary", {}
    if name == "list_stations": return "/api/v1/stations", args
    if name == "get_dashboard_overview": return "/api/v1/dashboard/overview", args
    if name == "get_alarm_rules": return "/api/v1/alarm-rules", args
    device_id = args.pop("device_id")
    if name == "get_device": return f"/api/v1/devices/{device_id}", {}
    if name == "get_prediction": return f"/api/v1/devices/{device_id}/prediction", {}
    if args.pop("mode") == "latest": return f"/api/v1/devices/{device_id}/telemetry/latest", {}
    if not all(args.get(k) for k in ("metric", "start", "end")):
        raise ValueError("历史遥测需要metric/start/end")
    return f"/api/v1/devices/{device_id}/telemetry", args
