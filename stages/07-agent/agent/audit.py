import asyncio
import json
from datetime import datetime, timezone


class Audit:
    def __init__(self, directory, secrets=()):
        self.directory = directory
        self.secrets = tuple(s for s in secrets if s)
        self.lock = asyncio.Lock()

    async def write(self, record, secrets=()):
        # Every path (including denial and cancellation) uses the same redaction.
        serialized = json.dumps({"at": datetime.now(timezone.utc).isoformat(), **record}, ensure_ascii=False)
        for secret in (*self.secrets, *secrets):
            if secret:
                serialized = serialized.replace(secret, "[REDACTED]")
        async with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / (datetime.now(timezone.utc).strftime("audit-%Y%m%d") + ".jsonl")
            with path.open("a", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
                stream.flush()
