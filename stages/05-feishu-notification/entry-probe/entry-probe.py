#!/usr/bin/env python3
"""公网入口探活与隧道健康检查。

只读：不改任何服务状态，只做探测并按需发飞书通知。
由 powersystem-entry-probe.timer 每分钟触发（Type=oneshot）。

判定逻辑：
- 公网 target 全部成功 -> 正常
- 连续 2 次失败才告警，避免单次抖动误报
- 持续故障每 30 分钟重复提醒一次
- 恢复时发一条恢复通知
- 同时统计主通道 frpc 最近 5 分钟的连接错误，能在入口彻底断之前提前发现
"""
import base64
import hashlib
import hmac
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR = Path("/var/lib/powersystem-probe")
STATE_FILE = STATE_DIR / "state.json"
WEBHOOK_FILE = "/etc/powersystem/feishu-webhook-url"
SIGN_FILE = "/etc/powersystem/feishu-sign-secret"

PUBLIC_TARGETS = (
    ("api", "https://www.cyancarp.cn/api/v1/ping"),
    ("web", "https://www.cyancarp.cn/"),
)
LOCAL_TARGET = "http://127.0.0.1:18080/api/v1/ping"
FRPC_UNIT = "powersystem-web-primary-frpc"

TIMEOUT = 10
FAIL_THRESHOLD = 2
REMIND_SECONDS = 1800
CST = timezone(timedelta(hours=8))


def stamp():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def opener():
    """显式禁用代理，避免系统代理干扰探活结果。"""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def feishu_sign(timestamp, secret):
    material = f"{timestamp}\n{secret}".encode()
    return base64.b64encode(hmac.new(material, digestmod=hashlib.sha256).digest()).decode()


def entry_card(recovered, details, hint):
    """入口探活与其他运维告警共用的状态、时间、证据、建议布局。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "green" if recovered else "red", "title": {
            "tag": "plain_text", "content": "PowerSystem · 公网入口 · 已恢复" if recovered else "PowerSystem · 公网入口异常"}},
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "plain_text", "content":
                    "状态\n已恢复" if recovered else "状态\n正在告警 · 紧急"}},
                {"is_short": True, "text": {"tag": "plain_text", "content": f"时间（北京）\n{stamp()}"}},
            ]},
            {"tag": "div", "text": {"tag": "plain_text", "content": "探测结果：\n" + "\n".join(details)}},
            {"tag": "div", "text": {"tag": "plain_text", "content":
                ("恢复说明：" if recovered else "处理建议：") + hint}},
            {"tag": "note", "elements": [{"tag": "plain_text", "content":
                "来源：VM117 公网入口探活；连续两次失败或主隧道异常时告警。"}]},
        ],
    }


def notify(card):
    """发送飞书卡片。告警失败不得影响探活本身。"""
    try:
        url = Path(WEBHOOK_FILE).read_text(encoding="utf-8").strip()
        secret = Path(SIGN_FILE).read_text(encoding="utf-8").strip()
        ts = int(time.time())
        body = json.dumps(
            {
                "timestamp": ts,
                "sign": feishu_sign(ts, secret),
                "msg_type": "interactive",
                "card": card,
            },
            ensure_ascii=False,
        ).encode()
        request = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with opener().open(request, timeout=10) as response:
            result = json.load(response)
        ok = result.get("code", result.get("StatusCode")) == 0
        if not ok:
            print("notify_rejected", file=sys.stderr)
        return ok
    except Exception as error:  # noqa: BLE001 - 告警链路失败只记录类型
        print(f"notify_failed: {type(error).__name__}", file=sys.stderr)
        return False


def probe(name, url, strict=True):
    """返回 (ok, 说明)。strict=True 时校验业务字段。"""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "powersystem-probe/1"})
        with opener().open(request, timeout=TIMEOUT) as response:
            status = response.status
            raw = response.read(4096)
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except Exception as error:  # noqa: BLE001
        return False, type(error).__name__
    if status != 200:
        return False, f"HTTP {status}"
    if not strict:
        return True, "200"
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001
        return False, "响应非 JSON"
    data = payload.get("data") or {}
    if payload.get("code") != 0:
        return False, f"code={payload.get('code')}"
    if data.get("telemetry_ready") is not True:
        return False, f"telemetry_ready={data.get('telemetry_ready')}"
    return True, "200"


def frpc_errors():
    """最近 5 分钟主通道连接失败次数。"""
    try:
        completed = subprocess.run(
            ["journalctl", "-u", FRPC_UNIT, "--since", "5 min ago", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15,
        )
        return completed.stdout.count("connect to server error")
    except Exception:  # noqa: BLE001
        return 0


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    counts = dict(state.get("counts") or {})
    failing = bool(state.get("failing"))
    fired_at = float(state.get("fired_at") or 0)

    lines = []
    public_ok = True
    for name, url in PUBLIC_TARGETS:
        ok, note = probe(name, url, strict=(name == "api"))
        counts[name] = 0 if ok else counts.get(name, 0) + 1
        lines.append(f"{name}: {note}" if ok else f"{name}: {note}（连续 {counts[name]} 次）")
        if not ok:
            public_ok = False

    local_ok, local_note = probe("local", LOCAL_TARGET)
    if not local_ok:
        lines.append(f"内网直连: {local_note}")

    errors = frpc_errors()
    tunnel_bad = errors >= 3
    if tunnel_bad:
        lines.append(f"主通道 frpc: 5 分钟内连接失败 {errors} 次")

    worst = max(counts.values()) if counts else 0
    abnormal = (not public_ok) or tunnel_bad
    alerted = False

    if abnormal and (worst >= FAIL_THRESHOLD or tunnel_bad):
        if (not failing) or (time.time() - fired_at > REMIND_SECONDS):
            if not local_ok:
                hint = "内网服务本身也可能异常，请优先检查 api 与容器状态。"
            elif not public_ok:
                hint = "内网服务正常，问题在公网入口：检查云侧防火墙的 7000 放行 IP 与 FRP 隧道。"
            else:
                hint = "入口尚未完全中断，但隧道主通道在反复重连，建议尽快检查出口 IP 是否变化。"
            notify(entry_card(False, lines, hint))
            failing, fired_at = True, time.time()
            alerted = True
    elif (not abnormal) and failing:
        notify(entry_card(True, lines, "本次探测已恢复正常；可继续观察下一轮结果。"))
        failing, fired_at = False, 0
        alerted = True

    STATE_FILE.write_text(
        json.dumps(
            {
                "failing": failing,
                "fired_at": fired_at,
                "counts": counts,
                "last_check": time.time(),
                "last_lines": lines,
                "alerted": alerted,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"ok": not abnormal, "alerted": alerted, "details": lines}, ensure_ascii=False))


if __name__ == "__main__":
    main()
