"""Describe actual tool execution, independently of model prose and prior turns."""
from datetime import datetime, timezone
from .tools import MODELS

BUSINESS = set(MODELS) - {"search_knowledge", "web_search"}


def iso(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (AttributeError, ValueError, TypeError):
        return None


def milliseconds(value):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return datetime.fromtimestamp(value / 1000, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def build_evidence(calls, cfg):
    business = [c for c in calls if c.get("name") in BUSINESS]
    success = sum(c.get("status") == "ok" for c in business)
    state = "not_queried" if not business else "queried" if success == len(business) else "partial_failed" if success else "failed"
    codes = {}
    for call in business:
        data = call.get("result")
        if call.get("status") != "ok" or not isinstance(data, dict): continue
        for row in data.get("list", []) if call["name"] == "list_devices" else [data]:
            if isinstance(row, dict) and row.get("device_code"):
                codes[row.get("id", row.get("device_id"))] = row["device_code"]
    cards = []
    for call in business:
        data = call.get("result") if isinstance(call.get("result"), dict) else {}
        device = call.get("arguments", {}).get("device_id", data.get("device_id"))
        card = {"tool": call["name"], "status": call.get("status"), "device_id": device,
                "device_code": data.get("device_code") or codes.get(device),
                "queried_at": call.get("collected_at"), "received_at": data.get("received_at"),
                "ts_ms": data.get("ts_ms"), "sampled_at": None, "freshness": "unknown"}
        sampled = milliseconds(data.get("ts_ms"))
        if call["name"] == "get_prediction":
            sampled = milliseconds(data.get("window_end_ms"))
            card["stale"] = data.get("stale")
            card["freshness"] = "stale" if data.get("stale") is True else "fresh" if data.get("stale") is False else "unknown"
        elif call["name"] == "get_telemetry" and call.get("arguments", {}).get("mode", "latest") == "latest":
            queried = iso(card["queried_at"])
            if sampled and queried:
                age = (queried - sampled).total_seconds()
                card["freshness"] = "future" if age < -cfg.future_skew_seconds else "stale" if age > cfg.telemetry_max_age_seconds else "fresh"
        if sampled: card["sampled_at"] = sampled.isoformat()
        if call.get("error"): card["error"] = call["error"]
        cards.append(card)
    return {"state": state, "success_count": success, "failed_count": len(business) - success, "cards": cards}
