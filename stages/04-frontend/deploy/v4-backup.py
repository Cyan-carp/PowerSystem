#!/usr/bin/env python3
"""V4 artifact snapshot/verify, composable with the existing encrypted full backup.

Reads one explicit RAG DSN file and one explicit artifact root. Every
assistant_artifacts row is hashed in full; bounded metadata plus the actual
file size/digest/state are recorded. capture fails closed unless a pause
evidence file asserts writes were stopped. verify is read-only by default.
DSNs, row bodies and evidence bodies are never printed.
"""
import argparse, asyncio, hashlib, json, os, re, stat, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit, parse_qsl

TOOL = "v4-backup"
VERSION = 1
STATUSES = ("ready", "expired", "unavailable")
UUID_LC = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1 << 20


class Fail(Exception):
    def __init__(self, kind, code):
        super().__init__(code); self.kind, self.code = kind, code


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_utc():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def load_dsn(path):
    """One-line postgres DSN; the value is never echoed."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise Fail("config", "database_url_file_unreadable")
    if not text or "\n" in text or "\r" in text:
        raise Fail("config", "database_url_file_invalid")
    try:
        parts = urlsplit(text); database = unquote(parts.path.lstrip("/")); _ = parts.port
    except ValueError:
        raise Fail("config", "database_url_invalid")
    options=parse_qsl(parts.query,keep_blank_values=True)
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname or not database or parts.fragment or any(k!='sslmode' or v not in ('disable','require','verify-ca','verify-full','prefer','allow') for k,v in options) or len(options)>1:
        raise Fail("config", "database_url_invalid")
    return text, database


def dsn_fingerprint(dsn):
    """Endpoint identity without userinfo or password."""
    parts = urlsplit(dsn)
    return digest(canon({"scheme": parts.scheme, "host": parts.hostname,
                         "port": parts.port, "database": unquote(parts.path.lstrip("/"))}))


def _time(value):
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def load_evidence(path, max_age_hours=24):
    """Caller-asserted pause evidence; only its digest and scope leave here."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        data = json.loads(text)
    except OSError:
        raise Fail("evidence", "pause_evidence_unreadable")
    except ValueError:
        raise Fail("evidence", "pause_evidence_invalid_json")
    if not isinstance(data, dict) or data.get("writes_paused") is not True:
        raise Fail("evidence", "writes_not_paused")
    scope = data.get("scope")
    if scope not in ("live", "restore"):
        raise Fail("evidence", "pause_scope_invalid")
    try:
        paused_at = _time(data.get("paused_at"))
        expires_at = _time(data["expires_at"]) if data.get("expires_at") else None
    except (ValueError, TypeError):
        raise Fail("evidence", "pause_time_invalid")
    current = now_utc()
    if paused_at > current + timedelta(minutes=5):
        raise Fail("evidence", "pause_time_in_future")
    if current - paused_at > timedelta(hours=max_age_hours):
        raise Fail("evidence", "pause_evidence_stale")
    if expires_at is not None and current > expires_at:
        raise Fail("evidence", "pause_evidence_expired")
    return {"sha256": digest(text), "scope": scope, "paused_at": iso(paused_at)}


def validate_rows(rows):
    problems, ids, keys = [], set(), set()
    for row in rows:
        ident = str(row.get("id"))
        def bad(reason): problems.append({"id": ident, "storage_key": row.get("storage_key"), "reason": reason})
        if not UUID_LC.fullmatch(ident) or str(uuid.UUID(ident)) != ident:
            bad("id_not_canonical")
        elif ident in ids:
            bad("duplicate_id")
        ids.add(ident)
        key = row.get("storage_key")
        if not isinstance(key, str) or not UUID_LC.fullmatch(key) or str(uuid.UUID(key)) != key:
            bad("storage_key_not_canonical")
        elif key in keys:
            bad("duplicate_storage_key")
        else:
            keys.add(key)
        size = row.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            bad("invalid_size")
        if not isinstance(row.get("sha256"), str) or not SHA256.fullmatch(row["sha256"]):
            bad("invalid_sha256")
        if row.get("status") not in STATUSES:
            bad("invalid_status")
    return problems


def unique_problems(problems):
    seen, result = set(), []
    for item in problems:
        marker = (item.get("id"), item.get("storage_key"), item.get("reason"))
        if marker not in seen:
            seen.add(marker); result.append(item)
    return result


def stream_digest(path, limit):
    hasher, total = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(CHUNK)
            if not block:
                break
            total += len(block)
            if total > limit:
                raise Fail("artifact", "file_exceeds_limit")
            hasher.update(block)
    return total, hasher.hexdigest()


def scan_files(rows, root, max_bytes, max_total_bytes):
    """Check every row against the actual root; symlinks/traversal/orphans fail."""
    root_path = Path(root)
    if not root_path.is_dir():
        raise Fail("artifact", "artifact_dir_missing")
    if root_path.is_symlink():
        raise Fail("artifact", "artifact_dir_symlink")
    base = os.path.realpath(root)
    problems, files, referenced = [], [], set()
    verified = absent = total = 0
    seen = set()
    for row in sorted(rows, key=lambda item: str(item.get("storage_key"))):
        key, status, ident = row.get("storage_key"), row.get("status"), str(row.get("id"))
        def bad(reason): problems.append({"id": ident, "storage_key": key, "reason": reason})
        if not isinstance(key, str) or not UUID_LC.fullmatch(key) or str(uuid.UUID(key)) != key:
            problems.append({"id": ident, "storage_key": key, "reason": "storage_key_not_canonical"})
            continue
        if status not in STATUSES:
            bad("invalid_status"); continue
        referenced.add(key)
        if key in seen:
            continue
        seen.add(key)
        path = os.path.join(base, key)
        entry = {"id": ident, "storage_key": key, "status": status,
                 "size": row.get("size"), "sha256": row.get("sha256"),
                 "expires_at": row.get("expires_at"), "file_removed_at": row.get("file_removed_at"),
                 "metadata_sha256": digest(canon(row))}
        if os.path.dirname(os.path.realpath(path)) != base or os.path.islink(path):
            bad("path_escape_or_symlink"); entry["file_state"] = "path_rejected"
            files.append(entry); continue
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            if status == "ready":
                bad("ready_file_missing"); entry["file_state"] = "missing"
            else:
                absent += 1; entry["file_state"] = "absent_expected"
            files.append(entry); continue
        except OSError:
            bad("file_unreadable"); entry["file_state"] = "unreadable"
            files.append(entry); continue
        if stat.S_ISLNK(before.st_mode):
            bad("path_escape_or_symlink"); entry["file_state"] = "path_rejected"
            files.append(entry); continue
        if not stat.S_ISREG(before.st_mode):
            bad("not_regular_file"); entry["file_state"] = "not_regular_file"
            files.append(entry); continue
        if row.get("file_removed_at") is not None:
            bad("file_present_after_removal")
        entry["file_size"] = before.st_size
        if before.st_size > max_bytes:
            bad("size_limit_exceeded"); entry["file_state"] = "oversize"
            total += before.st_size; files.append(entry); continue
        try:
            actual_size, actual_sha = stream_digest(path, max_bytes)
        except Fail:
            bad("size_limit_exceeded"); entry["file_state"] = "oversize"
            files.append(entry); continue
        except OSError:
            bad("file_unreadable"); entry["file_state"] = "unreadable"
            files.append(entry); continue
        try:
            after = os.lstat(path)
        except OSError:
            bad("file_unreadable"); entry["file_state"] = "unreadable"
            files.append(entry); continue
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            bad("file_changed_during_read"); entry["file_state"] = "changed_during_read"
        total += actual_size
        entry["file_size"], entry["file_sha256"] = actual_size, actual_sha
        size_ok = actual_size == row.get("size")
        digest_ok = actual_sha == row.get("sha256")
        if status == "ready":
            if not size_ok: bad("size_mismatch")
            if not digest_ok: bad("digest_mismatch")
            entry["file_state"] = "verified" if size_ok and digest_ok else "mismatch"
            verified += 1 if size_ok and digest_ok else 0
        elif status == "expired":
            if not size_ok: bad("size_mismatch")
            if not digest_ok: bad("digest_mismatch")
            entry["file_state"] = "expired_file_verified" if size_ok and digest_ok else "mismatch"
            verified += 1 if size_ok and digest_ok else 0
        else:  # unavailable already records a known integrity failure
            entry["file_state"] = "unavailable_file_verified" if size_ok and digest_ok else "unavailable_mismatch_expected"
            verified += 1 if size_ok and digest_ok else 0
        files.append(entry)
    try:
        entries = list(os.scandir(base))
    except OSError:
        raise Fail("artifact", "artifact_dir_unreadable")
    for item in sorted(entries, key=lambda value: value.name):
        if item.name in referenced:
            continue
        if item.is_symlink():
            problems.append({"id": "-", "storage_key": item.name, "reason": "unreferenced_symlink"})
        elif item.is_dir(follow_symlinks=False):
            problems.append({"id": "-", "storage_key": item.name, "reason": "unexpected_directory"})
        elif item.is_file(follow_symlinks=False):
            problems.append({"id": "-", "storage_key": item.name, "reason": "unreferenced_file"})
            try: total += item.stat(follow_symlinks=False).st_size
            except OSError: pass
        else:
            problems.append({"id": "-", "storage_key": item.name, "reason": "unexpected_entry"})
    if total > max_total_bytes:
        problems.append({"id": "-", "storage_key": "-", "reason": "total_size_limit_exceeded"})
    return {"rows": len(rows), "verified": verified, "absent_expected": absent,
            "total_bytes": total, "files": files, "problems": problems}


async def _read_snapshot(url, expect_database, max_rows, timeout):
    try:
        import asyncpg
    except ImportError:
        raise Fail("config", "asyncpg_unavailable")
    try:
        conn = await asyncpg.connect(url, timeout=30, command_timeout=timeout)
    except Exception:
        raise Fail("database", "connect_failed")

    async def read():
        try:
            database = await conn.fetchval("SELECT current_database()")
            if expect_database and database != expect_database:
                raise Fail("database", "unexpected_database")
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                await conn.execute("SET LOCAL TIME ZONE 'UTC'")
                fetched = await conn.fetch(
                    "SELECT to_jsonb(t)::text FROM public.assistant_artifacts AS t "
                    "ORDER BY storage_key LIMIT $1", max_rows + 1)
            if len(fetched) > max_rows:
                raise Fail("database", "row_limit_exceeded")
            rows = [json.loads(item[0]) for item in fetched]
            return {"database": database, "rows": rows, "count": len(rows),
                    "table_sha256": digest("\n".join(sorted(digest(canon(row)) for row in rows)))}
        except Fail:
            raise
        except Exception:
            raise Fail("database", "query_failed")

    try:
        return await read()
    finally:
        await conn.close()


def read_snapshot(url, expect_database, max_rows, timeout):
    return asyncio.run(_read_snapshot(url, expect_database, max_rows, timeout))


def snapshot_input(args):
    if args.rows_file:
        try:
            path=Path(args.rows_file)
            if path.stat().st_size>256<<20:raise Fail('database','metadata_input_limit')
            value=json.loads(path.read_text(encoding='utf-8'))
            database,rows=value['database'],value['rows']
            if not isinstance(database,str) or not isinstance(rows,list) or any(not isinstance(r,dict) for r in rows):raise ValueError()
            if args.expect_database and database!=args.expect_database:raise Fail('database','unexpected_database')
            if len(rows)>args.max_rows:raise Fail('database','row_limit_exceeded')
            return dict(database=database,rows=rows,count=len(rows),table_sha256=digest('\n'.join(sorted(digest(canon(row)) for row in rows))))
        except Fail:raise
        except (OSError,ValueError,KeyError,TypeError):raise Fail('database','metadata_input_invalid') from None
    dsn,_=load_dsn(args.database_url_file)
    return read_snapshot(dsn,args.expect_database,args.max_rows,args.timeout)


def check_new_output(path, artifact_root=None):
    target = Path(path)
    if target.exists():
        raise Fail("output", "output_exists")
    if not target.parent.is_dir():
        raise Fail("output", "output_parent_missing")
    if artifact_root is not None:
        try:
            if target.resolve().is_relative_to(Path(artifact_root).resolve()):
                raise Fail("output", "output_inside_artifact_dir")
        except OSError:
            raise Fail("output", "output_path_invalid")
    return target


def write_new_file(target, text):
    try:
        with open(target, "x", encoding="utf-8") as stream:
            stream.write(text)
    except FileExistsError:
        raise Fail("output", "output_exists")
    except OSError:
        raise Fail("output", "output_unwritable")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def new_output_dir(path, artifact_root):
    target = check_new_output(path, artifact_root)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError:
        raise Fail("output", "output_exists")
    except OSError:
        raise Fail("output", "output_unwritable")
    return target


def manifest_text(report):
    return json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def emit(report, target):
    text = manifest_text(report)
    if target is None:
        sys.stdout.write(text); return
    write_new_file(target, text)


def compare(expected, snap, scan):
    """Pure manifest comparison used by verify and its tests."""
    differences = []
    declared = expected.get("artifacts")
    if not isinstance(declared, dict) or not isinstance(declared.get("files"), list):
        return [{"storage_key": "-", "reason": "manifest_artifacts_invalid"}]
    if snap["count"] != declared.get("rows"):
        differences.append({"storage_key": "-", "reason": "row_count_mismatch"})
    if snap["table_sha256"] != declared.get("table_sha256"):
        differences.append({"storage_key": "-", "reason": "table_digest_mismatch"})
    want = {}
    for entry in declared["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("storage_key"), str):
            differences.append({"storage_key": "-", "reason": "manifest_entry_invalid"})
        else:
            want[entry["storage_key"]] = entry
    have = {entry.get("storage_key"): entry for entry in scan["files"]}
    for key in sorted(set(want) | set(have)):
        if key not in have:
            differences.append({"storage_key": key, "reason": "artifact_missing_now"})
        elif key not in want:
            differences.append({"storage_key": key, "reason": "artifact_unexpected_now"})
        else:
            old, new = want[key], have[key]
            if old.get("metadata_sha256") != new.get("metadata_sha256"):
                differences.append({"storage_key": key, "reason": "metadata_mismatch"})
            if old.get("file_state") != new.get("file_state"):
                differences.append({"storage_key": key, "reason": "file_state_mismatch"})
            if old.get("file_size") != new.get("file_size"):
                differences.append({"storage_key": key, "reason": "file_size_mismatch"})
            if old.get("file_sha256") != new.get("file_sha256"):
                differences.append({"storage_key": key, "reason": "file_digest_mismatch"})
    for problem in scan["problems"]:
        differences.append({"storage_key": problem.get("storage_key"), "reason": problem.get("reason")})
    return differences


def run_capture(args):
    evidence = load_evidence(args.pause_evidence, args.max_evidence_age_hours)
    output = check_new_output(args.output, args.artifact_dir)
    first = snapshot_input(args)
    problems = validate_rows(first["rows"])
    scan = scan_files(first["rows"], args.artifact_dir, args.max_bytes, args.max_total_bytes)
    second = snapshot_input(args)
    if second["table_sha256"] != first["table_sha256"] or second["count"] != first["count"]:
        problems.append({"id": "-", "storage_key": "-", "reason": "snapshot_changed_during_capture"})
    if load_evidence(args.pause_evidence, args.max_evidence_age_hours)["sha256"] != evidence["sha256"]:
        problems.append({"id": "-", "storage_key": "-", "reason": "pause_evidence_changed"})
    problems.extend(scan["problems"])
    problems = unique_problems(problems)
    report = {
        "tool": TOOL, "version": VERSION, "mode": "capture",
        "status": "ok" if not problems else "failed",
        "created_at": iso(now_utc()),
        "database": {"name": first["database"], "input": "postgres_export" if args.rows_file else "direct_connection"},
        "artifact_dir": os.path.abspath(args.artifact_dir),
        "evidence": evidence,
        "consistency": {
            "basis": "writes_paused_evidence",
            "production_consistency_claimed": False,
            "note": "the pause is asserted by the evidence caller; this tool does not verify the pause mechanism",
        },
        "limits": {"max_bytes": args.max_bytes, "max_total_bytes": args.max_total_bytes,
                   "max_rows": args.max_rows},
        "artifacts": {
            "rows": scan["rows"], "verified": scan["verified"],
            "absent_expected": scan["absent_expected"], "total_bytes": scan["total_bytes"],
            "table_sha256": first["table_sha256"], "files": scan["files"],
        },
        "problems": problems[:500],
        "problems_truncated": len(problems) > 500,
    }
    target = new_output_dir(output, args.artifact_dir)
    write_new_file(target / "manifest.json", manifest_text(report))
    sys.stdout.write(json.dumps({"tool": TOOL, "mode": "capture", "status": report["status"],
                                 "rows": scan["rows"], "verified": scan["verified"],
                                 "problems": len(problems)}, sort_keys=True) + "\n")
    return 0 if report["status"] == "ok" else 1


def run_verify(args):
    try:
        expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    except OSError:
        raise Fail("expected", "expected_unreadable")
    except ValueError:
        raise Fail("expected", "expected_invalid_json")
    if expected.get("tool") != TOOL or expected.get("version") != VERSION:
        raise Fail("expected", "expected_tool_mismatch")
    if expected.get("status") != "ok":
        raise Fail("expected", "expected_not_ok")
    if not isinstance(expected.get("artifacts"), dict) or not isinstance(expected.get("evidence"), dict):
        raise Fail("expected", "expected_structure_invalid")
    output = check_new_output(args.output, args.artifact_dir) if args.output else None
    if args.offline:
        basis = "offline_copy_not_production"
    else:
        evidence = load_evidence(args.pause_evidence, args.max_evidence_age_hours)
        if evidence["sha256"] != (expected.get("evidence") or {}).get("sha256"):
            raise Fail("evidence", "pause_evidence_mismatch")
        basis = "writes_paused_evidence"
    snap = snapshot_input(args)
    row_problems = validate_rows(snap["rows"])
    scan = scan_files(snap["rows"], args.artifact_dir, args.max_bytes, args.max_total_bytes)
    differences = compare(expected, snap, scan)
    differences.extend({"storage_key": item.get("storage_key"), "reason": item.get("reason")}
                       for item in row_problems)
    differences = unique_problems(differences)
    report = {
        "tool": TOOL, "version": VERSION, "mode": "verify",
        "status": "ok" if not differences else "failed",
        "checked_at": iso(now_utc()),
        "capture_created_at": expected.get("created_at"),
        "capture_scope": (expected.get("evidence") or {}).get("scope"),
        "verification_basis": basis,
        "production_consistency_claimed": False,
        "artifacts": {"rows": scan["rows"], "verified": scan["verified"],
                      "absent_expected": scan["absent_expected"], "total_bytes": scan["total_bytes"]},
        "differences": differences[:500],
        "differences_truncated": len(differences) > 500,
    }
    emit(report, output)
    return 0 if not differences else 1


def main(argv=None):
    parser = argparse.ArgumentParser(prog=TOOL, description="V4 artifact snapshot/verify.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "verify"):
        item = sub.add_parser(name)
        source=item.add_mutually_exclusive_group(required=True)
        source.add_argument("--database-url-file")
        source.add_argument("--rows-file", help="private PostgreSQL JSON export from stopped-writer backup/restore orchestration")
        item.add_argument("--artifact-dir", required=True)
        item.add_argument("--pause-evidence")
        item.add_argument("--expect-database")
        item.add_argument("--max-bytes", type=int, default=32 << 20)
        item.add_argument("--max-total-bytes", type=int, default=8 << 30)
        item.add_argument("--max-rows", type=int, default=200000)
        item.add_argument("--max-evidence-age-hours", type=float, default=24)
        item.add_argument("--timeout", type=float, default=150)
        if name == "capture":
            item.add_argument("--output", required=True)
        else:
            item.add_argument("--expected", required=True)
            item.add_argument("--output")
            item.add_argument("--offline", action="store_true",
                              help="verify an offline copy; never claims production consistency")
    args = parser.parse_args(argv)
    try:
        if args.max_bytes <= 0 or args.max_total_bytes <= 0 or args.max_rows <= 0 or args.timeout <= 0:
            raise Fail("config", "limit_invalid")
        if args.command == "capture":
            if not args.pause_evidence:
                raise Fail("evidence", "pause_evidence_required")
            return run_capture(args)
        if not args.offline and not args.pause_evidence:
            raise Fail("evidence", "pause_evidence_required")
        return run_verify(args)
    except Fail as error:
        sys.stdout.write(json.dumps({"tool": TOOL, "status": "failed",
                                     "error": {"type": error.kind, "code": error.code}},
                                    sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
