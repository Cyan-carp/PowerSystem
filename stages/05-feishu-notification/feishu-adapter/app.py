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


def message(payload: dict) -> str:
    alerts = payload.get("alerts", [])
    if not isinstance(alerts, list) or not alerts:
        raise ValueError("Alertmanager alerts missing")
    lines = ["PowerSystem 监控告警"]
    for alert in alerts[:20]:
        if not isinstance(alert, dict):
            raise ValueError("invalid alert")
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        state = "恢复" if alert.get("status") == "resolved" else "触发"
        name = str(labels.get("alertname", "unknown"))[:100]
        severity = str(labels.get("severity", "unknown"))[:40]
        summary = str(annotations.get("summary", ""))[:200]
        when = str(alert.get("endsAt") if state == "恢复" else alert.get("startsAt"))[:40]
        lines.append(f"{state} | {name} | {severity} | {when}\n{summary}")
    if len(alerts) > 20:
        lines.append(f"另有 {len(alerts) - 20} 条告警，请查看 Alertmanager")
    return "\n".join(lines)


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


def business_message(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("invalid business event")
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
    name = "AI 故障风险" if category == "prediction_risk" else "设备越限"
    metric = METRIC_LABELS.get(payload["metric"], payload["metric"])
    level = LEVEL_LABELS.get(payload["level"], payload["level"])
    style, action = STATE_STYLES[state]
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
                                    "content": f"触发时记录值 {highlight} ／ 触发阈值 {threshold:.3f}"}},
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
