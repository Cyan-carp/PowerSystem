"""Credential-safe local configuration and provider capability preflight."""
import asyncio
import json
import os
import sys
from urllib.parse import quote
import httpx
from .config import Config
from .llm import Provider, ProviderError


async def probe():
    config = Config.load()
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
        async with asyncio.timeout(config.total_timeout):
            result = await Provider(config, client).probe()
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agent.cli validate|probe|redis-url")
    command = sys.argv[1]
    try:
        if command == "redis-url":
            # Only captured by the PowerShell loader; do not invoke interactively.
            print("redis://:" + quote(os.environ["REDIS_PASSWORD"], safe="") + "@127.0.0.1:6379/0")
        elif command == "validate":
            Config.load()
            print("configuration valid; credentials not displayed")
        elif command == "probe":
            asyncio.run(probe())
        else:
            raise ValueError()
    except (ProviderError, TimeoutError, ValueError, KeyError) as error:
        # ProviderError contains a controlled code, never the vendor response.
        reason = str(error) if isinstance(error, ProviderError) else type(error).__name__
        print("preflight failed: " + reason + "; verify private configuration and provider capability", file=sys.stderr)
        raise SystemExit(1)
