import json
from datetime import datetime, timezone
from urllib.parse import urlencode
import httpx
from pydantic import Field, StrictInt, ValidationError
from typing import Literal
from .schemas import StrictModel, Evidence


def now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone required")
    return parsed


class Page(StrictModel):
    page: StrictInt = Field(default=1, ge=1)
    page_size: StrictInt = Field(default=20, ge=1, le=100)


class Devices(Page):
    group_name: str | None = Field(default=None, max_length=64)
    keyword: str | None = Field(default=None, max_length=128)


class Device(StrictModel):
    device_id: StrictInt = Field(gt=0)


class Telemetry(Device):
    mode: Literal["latest", "history"] = "latest"
    metric: Literal["voltage", "current", "temperature", "power"] | None = None
    start: str | None = None
    end: str | None = None


class Alarms(Page):
    device_id: StrictInt | None = Field(default=None, gt=0)
    status: Literal["unhandled", "acked", "recovered"] | None = None
    level: Literal["urgent", "major", "minor"] | None = None
    start: str | None = None
    end: str | None = None


class Alarm(StrictModel):
    alarm_id: StrictInt = Field(gt=0)


class Empty(StrictModel):
    pass


MODELS = {"list_devices": Devices, "get_telemetry": Telemetry, "get_prediction": Device,
          "list_alarms": Alarms, "get_alarm_detail": Alarm, "get_dashboard_summary": Empty}
DESCRIPTIONS = {
    "list_devices": "分页查询设备；group_name 精确分组，keyword 匹配设备名称或编码。",
    "get_telemetry": "查询设备最新值或指定指标历史；history 必须提供带时区 start/end，最长24小时。",
    "get_prediction": "查询设备模型概率、阈值、模型来源及过期状态。",
    "list_alarms": "分页查询告警；device_id、status、level、start/end 可选，时间依据 triggered_at。",
    "get_alarm_detail": "查询单条告警详情，不确认或改变告警。",
    "get_dashboard_summary": "查询全部可见演示设备汇总；动态值可能包含离线设备影响。",
}
TOOL_SCHEMAS = [{"type": "function", "function": {"name": name, "description": DESCRIPTIONS[name],
                "parameters": model.model_json_schema()}} for name, model in MODELS.items()]


def route(name, arguments):
    if name not in MODELS:
        raise ValueError("unknown_tool")
    args = MODELS[name].model_validate(arguments).model_dump(exclude_none=True)
    if name in ("get_telemetry", "list_alarms"):
        start, end = args.get("start"), args.get("end")
        for value in (start, end):
            if value is not None:
                parse_time(value)
        if start and end and parse_time(end) <= parse_time(start):
            raise ValueError("invalid_range")
    if name == "list_devices":
        return "/api/v1/devices", args
    if name == "list_alarms":
        return "/api/v1/alarms", args
    if name == "get_alarm_detail":
        return f"/api/v1/alarms/{args['alarm_id']}", {}
    if name == "get_dashboard_summary":
        return "/api/v1/dashboard/summary", {}
    device = args.pop("device_id")
    if name == "get_prediction":
        return f"/api/v1/devices/{device}/prediction", {}
    mode = args.pop("mode")
    if mode == "latest":
        if args:
            raise ValueError("latest_has_history_arguments")
        return f"/api/v1/devices/{device}/telemetry/latest", {}
    if not all(k in args for k in ("start", "end", "metric")):
        raise ValueError("history_arguments_required")
    if (parse_time(args["end"]) - parse_time(args["start"])).total_seconds() > 86400:
        raise ValueError("history_range_exceeds_24h")
    return f"/api/v1/devices/{device}/telemetry", args


def normalize(name, data):
    status, stamp = "ok", None
    if not isinstance(data, dict):
        raise ValueError("invalid_backend_data")
    data = dict(data)
    if name in ("list_devices", "list_alarms"):
        data["complete"] = data["page"] == 1 and len(data["list"]) == data["total"]
        stamp = now() if name == "list_devices" else None
        if not data["list"]:
            status = "no_data"
        elif name == "list_alarms":
            stamp = max(row["triggered_at"] for row in data["list"])
    elif name == "get_telemetry" and "points" in data:
        points = data["points"]
        values = [float(row[1]) for row in points if row[1] is not None]
        count = len(points)
        indices = sorted({round(i * (count - 1) / 99) for i in range(100)}) if count > 100 else range(count)
        data.update(original_points=count, compressed=count > 100,
                    statistics={"count": len(values), "min": min(values) if values else None,
                                "max": max(values) if values else None,
                                "mean": sum(values)/len(values) if values else None},
                    points=[points[i] for i in indices])
        stamp = str(points[-1][0]) if points else None
        if not values:
            status = "no_data"
    elif name == "get_telemetry":
        stamp = datetime.fromtimestamp(data["ts_ms"]/1000, timezone.utc).isoformat()
        if (datetime.now(timezone.utc) - parse_time(data["received_at"])).total_seconds() > 15:
            status = "stale"
    elif name == "get_prediction":
        stamp = datetime.fromtimestamp(data["window_end_ms"]/1000, timezone.utc).isoformat()
        if data["stale"]:
            status = "stale"
    elif name == "get_alarm_detail":
        stamp = data["triggered_at"]
    else:
        stamp = now()
        data["time_note"] = "汇总查询时间；不代表所有设备同时采样。"
    return status, stamp, data


class ToolDenied(Exception):
    def __init__(self, status):
        self.status = status


class Tools:
    def __init__(self, config, client):
        self.config, self.client = config, client

    async def execute(self, name, arguments, jwt, evidence_id):
        source, status, stamp, data = "", "invalid_arguments", None, {}
        try:
            path, params = route(name, arguments)
        except (ValueError, ValidationError, TypeError):
            return Evidence(id=evidence_id, tool=name, status=status, source=source,
                            collected_at=now(), data_time=stamp, data=data)
        source = path + ("?" + urlencode(params) if params else "")
        try:
            async with self.client.stream("GET", self.config.backend_url + path, params=params,
                                          headers={"Authorization": "Bearer " + jwt}, timeout=self.config.tool_timeout) as response:
                if response.status_code in (401, 403):
                    raise ToolDenied(response.status_code)
                if response.status_code != 200:
                    status = "no_data" if response.status_code == 404 else "backend_unavailable"
                else:
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1048576:
                            raise ValueError("response_too_large")
                    envelope = json.loads(raw)
                    if envelope["code"] != 0:
                        raise ValueError()
                    status, stamp, data = normalize(name, envelope["data"])
                    serialized = json.dumps(data, ensure_ascii=False)
                    for secret in (jwt, self.config.api_key, self.config.service_token, self.config.monitor_token, self.config.search_key):
                        if secret:
                            serialized = serialized.replace(secret, "[REDACTED]")
                    data = json.loads(serialized)
            if len(json.dumps(data, ensure_ascii=False)) > 60000:
                status, data = "response_too_large", {}
        except httpx.HTTPError:
            status = "backend_unavailable"
        except (ValueError, KeyError, TypeError, OverflowError):
            status, data = "invalid_backend_response", {}
        return Evidence(id=evidence_id, tool=name, status=status, source=source,
                        collected_at=now(), data_time=stamp, data=data)
