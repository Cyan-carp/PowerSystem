"""Read-only soak sampler for the live demo fleet.

Each round requests ``/api/v1/dashboard/overview`` for the ``all``,
``business`` and ``baseline`` scopes and appends one JSONL record per scope.
The only remote write is the login used to obtain a token; tokens, passwords
and response bodies are never written to disk or stdout.

A failed request never ends the observation: it is recorded as a short,
redacted error label and the next round continues.  A 401 triggers at most one
re-login and one retry for that scope.  The total run time is bounded by
``time.monotonic()``, so slow or failing requests cannot extend the run.
"""

import argparse
import json
import math
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

TIMEOUT_SECONDS = 10
SCOPES = ("all", "business", "baseline")
EXPECTED_DEVICES = {"all": 143, "business": 140, "baseline": 3}
USERNAME = "demo-operator"
MIN_DURATION, MAX_DURATION = 1, 86400
MIN_INTERVAL, MAX_INTERVAL = 1, 300


class SampleError(Exception):
    """Short, non-secret failure label."""

    def __init__(self, label):
        super().__init__(label)
        self.label = label


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def as_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def request_json(base, path, token=None, payload=None):
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise SampleError("http_status_%d" % exc.code) from None
    except (TimeoutError, socket.timeout):
        raise SampleError("timeout") from None
    except (urllib.error.URLError, OSError):
        raise SampleError("network_error") from None
    if status != 200:
        raise SampleError("http_status_%d" % status)
    try:
        body = json.loads(raw)
    except ValueError:
        raise SampleError("invalid_json") from None
    if not isinstance(body, dict):
        raise SampleError("invalid_response")
    code = body.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise SampleError("invalid_response")
    if code != 0:
        raise SampleError("business_code_%d" % code)
    data = body.get("data")
    if not isinstance(data, dict):
        raise SampleError("invalid_response")
    return data


def login(base, password):
    data = request_json(base, "/api/v1/auth/login",
                        payload={"username": USERNAME, "password": password})
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise SampleError("login_response_invalid")
    return token


def read_password(path):
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise SampleError("password_file_unreadable") from None
    if not password:
        raise SampleError("password_file_empty")
    return password


def overview_payload(base, scope, token):
    path = "/api/v1/dashboard/overview?" + urllib.parse.urlencode({"scope": scope})
    data = request_json(base, path, token)
    expected = as_int(data.get("expected_device_count"))
    registered = as_int(data.get("registered_device_count"))
    observed = as_int(data.get("observed_device_count"))
    partial = data.get("partial")
    if expected is None or registered is None or observed is None or not isinstance(partial, bool):
        raise SampleError("invalid_response")
    return {"expected": expected, "registered": registered,
            "observed": observed, "partial": partial}


def attempt_overview(base, scope, token):
    started = time.monotonic()
    try:
        data = overview_payload(base, scope, token)
    except SampleError as exc:
        return None, exc.label, round((time.monotonic() - started) * 1000.0, 1)
    return data, None, round((time.monotonic() - started) * 1000.0, 1)


def sample_scope(base, scope, session):
    data, label, latency = attempt_overview(base, scope, session["token"])
    if label == "http_status_401":
        try:
            session["token"] = login(base, session["password"])
        except SampleError:
            label = "relogin_failed"
        else:
            session["relogins"] += 1
            data, label, latency = attempt_overview(base, scope, session["token"])
            if label == "http_status_401":
                label = "http_status_401_after_relogin"
    record = {"scope": scope, "latency_ms": latency}
    if label is not None:
        record.update({"expected": None, "registered": None, "observed": None,
                       "partial": None, "error": label})
        return record
    record.update(data)
    record["error"] = None
    return record


def sample_round(base, session):
    stamp = utc_now()
    records = []
    for scope in SCOPES:
        record = {"ts": stamp}
        record.update(sample_scope(base, scope, session))
        records.append(record)
    return records


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return round(ordered[index], 1)


def coverage_incomplete(record):
    """Do not accept a manifest/registration count that silently shrank."""
    expected = EXPECTED_DEVICES[record["scope"]]
    return (record["expected"] != expected or record["registered"] != expected
            or record["observed"] != expected or record["partial"])


def run_soak(base, password, duration, interval, output_dir):
    started_at = utc_now()
    start = time.monotonic()
    deadline = start + duration
    session = {"token": login(base, password), "password": password, "relogins": 0}
    samples_path = output_dir / "samples.jsonl"
    latencies = []
    shortfall = {scope: 0 for scope in SCOPES}
    samples_total = 0
    errors = 0
    rounds = 0
    streak = 0
    max_streak = 0
    end_reason = "duration_elapsed"
    try:
        with samples_path.open("x", encoding="utf-8") as handle:
            while time.monotonic() < deadline:
                records = sample_round(base, session)
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                rounds += 1
                failed = 0
                for record in records:
                    samples_total += 1
                    if record["error"] is not None:
                        errors += 1
                        failed += 1
                        continue
                    latencies.append(record["latency_ms"])
                    if coverage_incomplete(record):
                        shortfall[record["scope"]] += 1
                # A round fails when any of its three scope requests failed.
                streak = streak + 1 if failed else 0
                max_streak = max(max_streak, streak)
                print("%s round=%d ok=%d err=%d" % (records[0]["ts"], rounds,
                                                    len(records) - failed, failed))
                sys.stdout.flush()
                next_at = start + rounds * interval
                wait = min(next_at, deadline) - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
    except KeyboardInterrupt:
        end_reason = "interrupted"
    ended_at = utc_now()
    actual = round(time.monotonic() - start, 1)
    summary = {
        "started_at": started_at,
        "ended_at": ended_at,
        "requested_duration_seconds": duration,
        "actual_duration_seconds": actual,
        "interval_seconds": interval,
        "rounds": rounds,
        "samples": samples_total,
        "errors": errors,
        "coverage_shortfall": shortfall,
        "max_consecutive_failed_rounds": max_streak,
        "relogin_count": session["relogins"],
        "latency_ms": {"p50": percentile(latencies, 0.50),
                       "p95": percentile(latencies, 0.95),
                       "max": round(max(latencies), 1) if latencies else None,
                       "count": len(latencies)},
        "result": ("pass" if end_reason == "duration_elapsed" and actual >= duration
                   and samples_total > 0 and errors == 0
                   and not any(shortfall.values()) else "anomaly"),
        "end_reason": end_reason,
    }
    with (output_dir / "summary.json").open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only soak sampler for the fleet overview API.",
        epilog="Requires a new --output-dir. Writes samples.jsonl and summary.json; "
               "never stores passwords, tokens or response bodies.")
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password-file", type=Path,
                        default=Path("/etc/powersystem/demo-password"))
    parser.add_argument("--duration", type=int, default=300,
                        help="total seconds, 1..%d (default 300)" % MAX_DURATION)
    parser.add_argument("--interval", type=int, default=10,
                        help="seconds between rounds, 1..%d (default 10)" % MAX_INTERVAL)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new directory; must not exist")
    args = parser.parse_args(argv)

    if not MIN_DURATION <= args.duration <= MAX_DURATION:
        print("error: duration_out_of_range", file=sys.stderr)
        return 2
    if not MIN_INTERVAL <= args.interval <= MAX_INTERVAL:
        print("error: interval_out_of_range", file=sys.stderr)
        return 2
    try:
        password = read_password(args.password_file)
    except SampleError as exc:
        print("error: %s" % exc.label, file=sys.stderr)
        return 1
    try:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        print("error: output_dir_exists", file=sys.stderr)
        return 1
    except OSError:
        print("error: output_dir_unwritable", file=sys.stderr)
        return 1
    try:
        summary = run_soak(args.base.rstrip("/"), password, args.duration,
                           args.interval, args.output_dir)
    except SampleError as exc:
        print("error: %s" % exc.label, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    print("result=%s end_reason=%s rounds=%d samples=%d errors=%d"
          % (summary["result"], summary["end_reason"], summary["rounds"],
             summary["samples"], summary["errors"]))
    return 0 if summary["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
