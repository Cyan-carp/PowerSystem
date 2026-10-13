"""Local offline + real PostgreSQL tests without printing connection credentials."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[3]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["NATURAL_TEST_POSTGRES_DSN"] = "postgresql://" + quote(env.get("POSTGRES_USER", "powersystem"), safe="") + ":" + quote(env["POSTGRES_PASSWORD"], safe="") + "@127.0.0.1:5432/" + env.get("POSTGRES_DB", "powersystem") + "?sslmode=disable"
    env.update(GOCACHE=str(ROOT/"artifacts/go-cache"), GOMODCACHE=str(ROOT/"artifacts/go-mod"), GOTELEMETRY="off")
    passed = True
    for name, cwd, command in [
        ("agent", ROOT, [sys.executable,"-X","utf8","-m","unittest","discover","-s","stages/07-agent/tests","-q"]),
        ("backend", ROOT/"stages/02-backend", ["go","test","-count=1","./..."]),
    ]:
        path = args.output/(name+".log")
        with path.open("w",encoding="utf-8") as log:
            result = subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT)
        # Logs stay private: no raw vendor errors or DSNs copied into chat.
        print(name, "passed" if result.returncode == 0 else "failed", "log:",path)
        passed = passed and result.returncode == 0
    return 0 if passed else 1


if __name__ == "__main__": raise SystemExit(main())
