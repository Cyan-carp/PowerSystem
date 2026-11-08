"""Internal Alertmanager webhook adapter for a signed Feishu group bot."""

import base64
import hashlib
import hmac
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def secret(path: str) -> str:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("private Feishu configuration is empty")
    return value


def sign(timestamp: int, signing_secret: str) -> str:
    material = f"{timestamp}\n{signing_secret}".encode()
    return base64.b64encode(hmac.new(material, digestmod=hashlib.sha256).digest()).decode()


MONITOR_NAMES = {
    "PowerSystemAPIDown": ("API 服务不可用", "检查 API 容器、健康状态和监控采集。"),
    "PowerSystemDiskHigh": ("磁盘空间不足", "检查占用增长和可用空间，按运维流程处理。"),
    "PowerSystemTelemetryProbeFailed": ("遥测健康检查失败", "检查数据库、缓存及遥测探针。"),
    "PowerSystemTelemetryInputStopped": ("遥测数据停止接收", "检查模拟器、MQTT 和网关输入。"),
    "PowerSystemTelemetryCoverageLow": ("设备遥测覆盖不足", "检查缺失设备和数据新鲜度。"),
    "PowerSystemTelemetryWorkerStalled": ("遥测处理停滞", "检查入库队列、处理任务和依赖状态。"),
    "PowerSystemTelemetryNoBusinessSuccess": ("入库业务提交中断", "检查待处理队列与业务提交失败记录。"),
    "PowerSystemTelemetryMaintenanceOverdue": ("遥测维护超时", "核对维护窗口和恢复进度。"),
    "PowerSystemTelemetryRegistryUnknown": ("设备登记状态不明", "检查设备登记和维护状态来源。"),
    "PowerSystemTelemetryRedisWriteStalled": ("遥测缓存写入停滞", "检查 Redis 写入及队列推进。"),
    "PowerSystemInboxExpiryBurst": ("入库样本集中逾期", "排查持续入库故障或处理能力；过期留档不等于成功。"),
    "PowerSystemInboxExpiryProbeBurst": ("入库过期受控演练", "这是受控演练，请勿当作正式业务故障。"),
    "PowerSystemInboxExpiryStalled": ("过期样本收尾停滞", "检查过期收尾任务及窗口外待处理行。"),
}

MONITOR_SUMMARIES = {
    "PowerSystemAPIDown": "监控连续超过 1 分钟无法采集 API。",
    "PowerSystemDiskHigh": "主机磁盘占用持续超过 80%。",
    "PowerSystemTelemetryProbeFailed": "遥测健康探针或依赖检查失败。",
    "PowerSystemTelemetryInputStopped": "预期设备的数据接收停止或尚未初始化。",
    "PowerSystemTelemetryCoverageLow": "登记设备出现缺数或数据变旧。",
    "PowerSystemTelemetryWorkerStalled": "遥测处理任务或待处理队列没有正常推进。",
    "PowerSystemTelemetryNoBusinessSuccess": "队列有待处理数据，但近期没有业务入库成功。",
    "PowerSystemTelemetryMaintenanceOverdue": "遥测维护超过预定观察时间。",
    "PowerSystemTelemetryRegistryUnknown": "设备登记或维护状态无法确认。",
    "PowerSystemTelemetryRedisWriteStalled": "队列有待处理数据，但缓存近期没有成功写入。",
    "PowerSystemInboxExpiryBurst": "15 分钟内至少 10 条正式样本超过 7 天，已过期留档。",
    "PowerSystemInboxExpiryProbeBurst": "15 分钟内至少 10 条受控演练样本已过期留档。",
    "PowerSystemInboxExpiryStalled": "超过 7 天的样本仍留在待处理队列超过 2 分钟。",
}


def monitor_field(label: str, value: str) -> dict:
    return {"is_short": True, "text": {"tag": "plain_text", "content": f"{label}\n{value}"}}


def message(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid Alertmanager payload")
    alerts = payload.get("alerts", [])
    if not isinstance(alerts, list) or not alerts:
        raise ValueError("Alertmanager alerts missing")
    elements = []
    for alert in alerts[:10]:
        if not isinstance(alert, dict):
            raise ValueError("invalid alert")
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        if not isinstance(labels, dict) or not isinstance(annotations, dict):
            raise ValueError("invalid alert details")
        state = "已恢复" if alert.get("status") == "resolved" else "正在告警"
        name = str(labels.get("alertname", "unknown"))[:100]
        title, action = MONITOR_NAMES.get(name, (name, "请查看监控平台中的告警详情。"))
        severity = {"critical": "紧急", "warning": "注意", "info": "提示"}.get(str(labels.get("severity", "")), "未标级别")
        when = str((alert.get("endsAt") if state == "已恢复" else alert.get("startsAt")) or "")[:40]
        occurred = beijing_time(when) if when else "时间未知"
        summary = MONITOR_SUMMARIES.get(name, str(annotations.get("summary", ""))[:200])
        instance = str(labels.get("instance", ""))[:100]
        elements.append({"tag": "div", "fields": [
            monitor_field("事件", title), monitor_field("状态 / 级别", f"{state} · {severity}"),
            monitor_field("时间（北京）", occurred), monitor_field("来源", instance or str(labels.get("job", "监控系统"))[:100]),
        ]})
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content":
            (f"说明：{summary}\n" if summary else "") +
            (f"恢复说明：已恢复；若反复出现，{action}" if state == "已恢复" else f"处理建议：{action}")}})
        elements.append({"tag": "hr"})
    if len(alerts) > 10:
        elements.append({"tag": "note", "elements": [{"tag": "plain_text", "content":
            f"另有 {len(alerts) - 10} 条，请到监控平台查看。"}]})
    single = alerts[0] if len(alerts) == 1 else None
    single_name = str((single.get("labels") or {}).get("alertname", "unknown")) if isinstance(single, dict) else ""
    headline = MONITOR_NAMES.get(single_name, (single_name, ""))[0] if single else f"监控告警 {len(alerts)} 项"
    firing = any(alert.get("status") != "resolved" for alert in alerts)
    critical = any((alert.get("labels") or {}).get("severity") == "critical" for alert in alerts)
    return {"config": {"wide_screen_mode": True}, "header": {
        "template": "red" if firing and critical else "orange" if firing else "green",
        "title": {"tag": "plain_text", "content": f"PowerSystem · {headline} · {'告警' if firing else '已恢复'}"[:100]}},
        "elements": elements}


LEVEL_LABELS = {"urgent": "🔴 紧急", "major": "🟠 重要", "minor": "🟡 一般"}
METRIC_LABELS = {"ai_failure_risk": "AI 故障风险", "temperature": "温度", "power": "功率",
                 "voltage": "电压", "current": "电流"}
STATE_STYLES = {
    "triggered": ("red", "🔴 触发告警"),
    "acknowledged": ("orange", "🟠 已确认"),
    "recovered": ("green", "✅ 已恢复"),
    "closed": ("grey", "⚪ 因配置变更关闭"),
}


def beijing_time(iso_text: str) -> str:
    """ISO 时间转北京时间可读格式；解析失败时回退原文。"""
    try:
        moment = datetime.fromisoformat(iso_text.replace("Z", "+00:00"))
        return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_text


def field(label: str, value: str) -> dict:
    return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}


def inbox_expiry_message(payload: dict) -> dict:
    expected = {"event_id", "category", "state", "rows", "first_inbox_id", "last_inbox_id", "occurred_at", "test_only"}
    if set(payload) != expected or payload.get("category") != "inbox_expiry" or payload.get("state") != "quarantined":
        raise ValueError("invalid inbox expiry event")
    if not isinstance(payload["test_only"], bool):
        raise ValueError("invalid inbox expiry test marker")
    first, last, rows = (payload[key] for key in ("first_inbox_id", "last_inbox_id", "rows"))
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (first, last, rows)):
        raise ValueError("invalid inbox expiry numbers")
    if first < 1 or last < first or rows < 1 or rows > last - first + 1:
        raise ValueError("invalid inbox expiry range")
    occurred = payload["occurred_at"]
    if not isinstance(occurred, str) or len(occurred) > 40:
        raise ValueError("invalid inbox expiry time")
    try:
        moment = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid inbox expiry time") from exc
    if moment.tzinfo is None:
        raise ValueError("inbox expiry time requires timezone")
    utc = moment.astimezone(timezone.utc)
    bucket = utc.replace(minute=utc.minute - utc.minute % 15, second=0, microsecond=0)
    event_id = ("inbox-expiry-test:" if payload["test_only"] else "inbox-expiry:") + bucket.strftime("%Y%m%d%H%M")
    if payload["event_id"] != event_id:
        raise ValueError("invalid inbox expiry event id")
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "red", "title": {"tag": "plain_text", "content":
            "PowerSystem · 入库过期受控演练" if payload["test_only"] else "PowerSystem · 入库样本过期留档"}},
        "elements": [
            {"tag": "div", "fields": [field("本时段已发现", f"至少 {rows} 条"), field("首批队列编号", f"{first}–{last}")]},
            {"tag": "div", "text": {"tag": "lark_md", "content":
                ("这些是带独立标记的受控演练样本，未进入正常业务处理；原行只作隔离试验记录。"
                 if payload["test_only"] else "这些样本超过 7 天仍未正常入库，已移出待处理队列并保留原记录；**不是处理成功，也没有自动补回**。")
                + "通知按 15 分钟合并，后续数量请看监控，并排查入库故障或处理能力。"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                f"事件 {event_id} · {beijing_time(occurred)}（北京时间）"}]},
        ],
    }


def business_message(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid business event")
    if payload.get("category") == "inbox_expiry":
        return inbox_expiry_message(payload)
    event_id = payload.get("event_id")
    category = payload.get("category")
    state = payload.get("state")
    alarm_id = payload.get("alarm_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"alarm:[1-9][0-9]*:(triggered|acknowledged|recovered|closed)", event_id):
        raise ValueError("invalid event id")
    if not isinstance(alarm_id, int) or isinstance(alarm_id, bool) or alarm_id < 1 or event_id != f"alarm:{alarm_id}:{state}":
        raise ValueError("invalid alarm id")
    if category not in ("device_alarm", "prediction_risk") or state not in ("triggered", "acknowledged", "recovered", "closed"):
        raise ValueError("invalid business state")
    fields = ("device_code", "station_code", "metric", "level", "occurred_at")
    if any(not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 80 for key in fields):
        raise ValueError("invalid business fields")
    if payload["level"] not in ("urgent", "major", "minor"):
        raise ValueError("invalid severity")
    value, threshold = payload.get("value"), payload.get("threshold")
    if any(isinstance(item, bool) or not isinstance(item, (float, int)) or not math.isfinite(item) for item in (value, threshold)):
        raise ValueError("invalid metric values")
    simulation = payload["metric"] == "simulation_scenario"
    name = "模拟演示告警" if simulation else "AI 故障风险" if category == "prediction_risk" else "设备越限"
    metric = METRIC_LABELS.get(payload["metric"], payload["metric"])
    level = LEVEL_LABELS.get(payload["level"], payload["level"])
    style, action = STATE_STYLES[state]
    if simulation and state == "recovered":
        action = "演示已结束"
    highlight = f"**{value:.3f}**" if state == "triggered" else f"{value:.3f}"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": style, "title": {"tag": "plain_text", "content": f"PowerSystem · {name} {action}"}},
        "elements": [
            {"tag": "div", "fields": [
                field("设备", payload["device_code"]),
                field("场站", payload["station_code"]),
                field("级别", level),
                field("指标", metric),
            ]},
            {"tag": "div", "text": {"tag": "lark_md",
                                    "content": "受控模拟演示，10分钟后自动结束；不代表实际物理故障或测量越限。" if simulation else f"触发时记录值 {highlight} ／ 触发阈值 {threshold:.3f}"}},
            {"tag": "hr"},
            {"tag": "note", "elements": [{"tag": "plain_text",
                                          "content": f"事件 {event_id} · {beijing_time(payload['occurred_at'])}（北京时间）"}]},
        ],
    }
    return card


def send(url: str, signing_secret: str, text: str | dict) -> None:
    timestamp = int(time())
    if isinstance(text, str):
        payload_content = {"msg_type": "text", "content": {"text": text}}
    else:
        payload_content = {"msg_type": "interactive", "card": text}
    body = json.dumps({"timestamp": timestamp, "sign": sign(timestamp, signing_secret),
                       **payload_content}, ensure_ascii=False).encode()
    request = Request(url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urlopen(request, timeout=8) as response:
        result = json.load(response)
    if result.get("code", result.get("StatusCode")) != 0:
        raise ValueError("Feishu rejected notification")


class Handler(BaseHTTPRequestHandler):
    webhook_url = ""
    signing_secret = ""
    business_token = ""

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        if self.path not in ("/alertmanager", "/business"):
            self.send_error(404)
            return
        if self.path == "/business" and not hmac.compare_digest(self.headers.get("X-PowerSystem-Token", ""), self.business_token):
            self.send_error(403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 1024 * 1024:
                raise ValueError("invalid payload size")
            payload = json.loads(self.rfile.read(length))
            text = business_message(payload) if self.path == "/business" else message(payload)
            send(self.webhook_url, self.signing_secret, text)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            # Do not log the webhook URL, secret, payload, or HTTP exception text.
            print(f"feishu_delivery_failed={type(exc).__name__}", flush=True)
            self.send_error(502)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"delivered")


if __name__ == "__main__":
    Handler.webhook_url = secret(os.getenv("FEISHU_WEBHOOK_FILE", "/run/secrets/feishu_webhook"))
    Handler.signing_secret = secret(os.getenv("FEISHU_SIGN_FILE", "/run/secrets/feishu_sign"))
    Handler.business_token = secret(os.getenv("FEISHU_BUSINESS_TOKEN_FILE", "/run/secrets/feishu_business_token"))
    parsed = urlparse(Handler.webhook_url)
    if parsed.scheme != "https" or parsed.hostname != "open.feishu.cn" or not parsed.path.startswith("/open-apis/bot/v2/hook/"):
        raise ValueError("invalid Feishu webhook URL")
    os.setgid(65532)
    os.setuid(65532)
    ThreadingHTTPServer(("0.0.0.0", 8091), Handler).serve_forever()
