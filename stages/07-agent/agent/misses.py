"""Append-only, redacted gap records; review decisions live in a separate file."""
import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path


def redact(value, secrets=()):
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(?:bearer\s+\S+|(?:password|token|api[_-]?key|secret|密码|密钥|账号|用户名)\s*[:=：]\s*\S+)", "[REDACTED]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", value)
    value = re.sub(r"(?:https?://)?(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?(?:/[^\s]*)?", "[ADDRESS]", value)
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", "[ACCOUNT]", value)
    value = re.sub(r"\b(?:INV|DEV|DEVICE)[-_][A-Za-z0-9_-]+\b", "[DEVICE]", value, flags=re.I)
    return value[:4000]


class Misses:
    def __init__(self, directory, secrets=()):
        self.directory, self.secrets = Path(directory), secrets
        self.lock = asyncio.Lock()

    async def write(self, record, secrets=()):
        def clean(value):
            if isinstance(value, str):
                return redact(value, (*self.secrets, *secrets))
            if isinstance(value, list):
                return [clean(v) for v in value]
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            return value
        value = clean(record)
        async with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.cleanup()
            stamp = datetime.now(timezone.utc)
            path = self.directory / (stamp.strftime("misses-%Y%m%d") + ".jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": stamp.isoformat(), **value}, ensure_ascii=False) + "\n")
                stream.flush()

    def cleanup(self, today=None):
        cutoff = (today or datetime.now(timezone.utc).date()) - timedelta(days=90)
        removed = 0
        for path in self.directory.glob("misses-????????.jsonl"):
            match = re.fullmatch(r"misses-(\d{8})\.jsonl", path.name)
            if not match or path.is_symlink():
                continue
            try:
                date = datetime.strptime(match[1], "%Y%m%d").date()
            except ValueError:
                continue
            if date < cutoff:
                path.unlink()
                removed += 1
        return removed
