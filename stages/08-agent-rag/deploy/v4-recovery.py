#!/usr/bin/env python3
"""V4 S09 read-only recovery snapshot: SHA-256 of public assistant_*/rag_* tables and artifact files.
One READ ONLY REPEATABLE READ transaction, SELECT only: no workers, approvals, scheduling or DB writes.
DSNs and row bodies are never printed; failures use a fixed envelope."""
import argparse, asyncio, hashlib, json, os, re, sys, uuid
from pathlib import Path

TOOL = "v4-recovery"
REQUIRED = ("assistant_runs", "assistant_run_events", "assistant_artifacts", "assistant_schedules",
            "assistant_schedule_grants", "assistant_schedule_slots", "assistant_schedule_receipts")
ART_FIELDS = ("id", "storage_key", "size", "sha256", "status")
TABLES_SQL = ("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND (table_name LIKE "
              "'assistant\\_%' OR table_name IN ('rag_sessions','rag_turns')) ORDER BY 1")
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
UUID_LC = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class Fail(Exception):
    def __init__(self, kind, code):
        super().__init__(code); self.kind, self.code = kind, code


def canon(o): return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def digest(t): return hashlib.sha256(t.encode("utf-8")).hexdigest()
def file_digest(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""): h.update(c)
    return h.hexdigest()


def load_url(path):
    """Loopback-only DSN whose database matches v4_s09_*_test; never echoed."""
    try: text = Path(path).read_text(encoding="utf-8").strip()
    except OSError: raise Fail("config", "database_url_file_unreadable")
    if not text or "\n" in text or "\r" in text: raise Fail("config", "database_url_file_invalid")
    from urllib.parse import unquote, urlsplit
    try: parts = urlsplit(text); db = unquote(parts.path.lstrip("/"))
    except ValueError: raise Fail("config", "database_url_invalid")
    if parts.scheme not in ("postgres", "postgresql") or parts.hostname != "127.0.0.1" or parts.query or parts.fragment:
        raise Fail("config", "database_url_not_isolated")
    if not re.match(r"^v4_s09_[a-z0-9_]+_test$", db): raise Fail("config", "database_not_isolated")
    return text


async def snapshot(url):
    try: import asyncpg
    except ImportError: raise Fail("config", "asyncpg_unavailable")
    try: conn = await asyncpg.connect(url, timeout=30, command_timeout=150)
    except Exception: raise Fail("database", "connect_failed")
    try:
        actual_db = await conn.fetchval("SELECT current_database()")
        if not re.fullmatch(r"v4_s09_[a-z0-9_]+_test", actual_db):
            raise Fail("config", "database_not_isolated")
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SET LOCAL TIME ZONE 'UTC'")
            names = [r["table_name"] for r in await conn.fetch(TABLES_SQL)]
            missing = [t for t in REQUIRED if t not in names]
            if not names or missing: raise Fail("schema", "missing_tables:" + ",".join(missing or ["assistant_*"]))
            tables, artifacts = [], []
            for name in names:
                if not IDENT.match(name): raise Fail("schema", "invalid_table_name")
                rows = [r[0] for r in await conn.fetch('SELECT to_jsonb(t)::text FROM public."%s" AS t' % name)]
                if name == "assistant_artifacts":
                    artifacts = [{k: json.loads(r).get(k) for k in ART_FIELDS} for r in rows]
                # Hash every column including owner/run/provenance/expiry.
                # Only the bounded file metadata projection leaves memory.
                hashes = sorted(digest(canon(json.loads(r))) for r in rows)
                tables.append({"table": name, "rows": len(rows), "sha256": digest("\n".join(hashes))})
    except Fail: raise
    except Exception: raise Fail("database", "query_failed")
    finally: await conn.close()
    return {"tables": tables, "artifacts": artifacts}


def check_files(rows, root):
    base = os.path.realpath(root)
    if not os.path.isdir(base): raise Fail("artifact", "artifact_dir_missing")
    checked, problems = 0, []
    for row in sorted(rows, key=lambda r: str(r.get("storage_key"))):
        key, status, ident = row.get("storage_key"), row.get("status"), str(row.get("id"))
        def bad(reason): problems.append({"id": ident, "reason": reason})
        if not isinstance(key, str) or not UUID_LC.match(key) or str(uuid.UUID(key)) != key:
            bad("storage_key_not_canonical"); continue
        path = os.path.join(base, key)
        if os.path.islink(path) or os.path.dirname(os.path.realpath(path)) != base:
            bad("path_escape_or_symlink"); continue
        if not os.path.isfile(path):
            if status == "ready": bad("ready_file_missing")
            continue
        try: size, actual = os.path.getsize(path), file_digest(path)
        except OSError: bad("file_unreadable"); continue
        if size != row.get("size") or actual != row.get("sha256"): bad("size_or_digest_mismatch")
        else: checked += 1
    return {"rows": len(rows), "checked": checked, "problems": problems}


def emit(report, target):
    text = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if target in (None, "-"): sys.stdout.write(text); return
    try: Path(target).write_text(text, encoding="utf-8")
    except OSError: raise Fail("output", "output_unwritable")


def diff(expected, current):
    diffs = []
    want = {t.get("table"): t for t in expected.get("tables") or []}
    have = {t["table"]: t for t in current["tables"]}
    for name in sorted(set(want) | set(have)):
        if name not in have: diffs.append({"table": name, "reason": "missing_now"})
        elif name not in want: diffs.append({"table": name, "reason": "unexpected_now"})
        elif want[name].get("rows") != have[name]["rows"]: diffs.append({"table": name, "reason": "row_count_mismatch"})
        elif want[name].get("sha256") != have[name]["sha256"]: diffs.append({"table": name, "reason": "digest_mismatch"})
    return diffs


def run(args):
    snap = asyncio.run(snapshot(load_url(args.database_url_file)))
    files = check_files(snap["artifacts"], args.artifact_dir)
    base = {"tool": TOOL, "artifact_dir": os.path.abspath(args.artifact_dir), "artifacts": files}
    if args.command == "capture":
        report = dict(base, mode="capture", tables=snap["tables"], status="ok" if not files["problems"] else "failed")
        emit(report, args.output); return 0 if report["status"] == "ok" else 1
    try: expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    except (OSError, ValueError): raise Fail("expected", "expected_unreadable")
    if expected.get("tool") != TOOL or expected.get("status") != "ok": raise Fail("expected", "expected_not_ok")
    diffs = diff(expected, snap)
    if files["problems"]: diffs.append({"table": "assistant_artifacts", "reason": "artifact_file_problems"})
    if expected.get("artifacts", {}).get("checked") != files["checked"]:
        diffs.append({"table": "assistant_artifacts", "reason": "artifact_file_count_mismatch"})
    report = dict(base, mode="verify", tables_checked=len(snap["tables"]), differences=diffs,
                  status="ok" if not diffs else "failed")
    emit(report, args.output); return 0 if not diffs else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog=TOOL, description="Read-only V4 S09 recovery snapshot/verify.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "verify"):
        p = sub.add_parser(name)
        p.add_argument("--database-url-file", required=True); p.add_argument("--artifact-dir", required=True)
        p.add_argument("--output", default="-")
        if name == "verify": p.add_argument("--expected", required=True)
    args = parser.parse_args(argv)
    try: return run(args)
    except Fail as err:
        emit({"tool": TOOL, "status": "failed", "error": {"type": err.kind, "code": err.code}}, getattr(args, "output", "-"))
        return 2


if __name__ == "__main__":
    sys.exit(main())
