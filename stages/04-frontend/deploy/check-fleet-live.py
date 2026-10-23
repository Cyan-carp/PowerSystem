"""Read-only consistency check for the live demo fleet."""

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TIMEOUT_SECONDS = 10
WAIT_SECONDS = 60
WAIT_INTERVAL_SECONDS = 5
BASELINE_CODES = {"INV-1001", "INV-1002", "INV-1003"}
EXPECTED_DEVICES = {"all": 143, "business": 140, "baseline": 3}
EXPECTED_STATIONS = {"all": 5, "business": 4, "baseline": 1}
PV04_DEVICES = 20
BUSINESS_PAGE_TWO = 40


class CheckError(Exception):
    """Short, non-secret failure label."""


def as_int(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def api(base, path, token=None, payload=None):
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
        raise CheckError("http_status_%d" % exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CheckError("network_error") from None
    if status != 200:
        raise CheckError("http_status_%d" % status)
    try:
        body = json.loads(raw)
    except ValueError:
        raise CheckError("invalid_json") from None
    if not isinstance(body, dict):
        raise CheckError("invalid_response")
    code = body.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise CheckError("invalid_response")
    if code != 0:
        raise CheckError("business_code_%d" % code)
    data = body.get("data")
    if not isinstance(data, dict):
        raise CheckError("invalid_response")
    return data


def list_path(resource, **params):
    return "/api/v1/%s?%s" % (resource, urllib.parse.urlencode(params))


def check_device_total(base, token, scope):
    data = api(base, list_path("devices", scope=scope, page=1, page_size=1), token)
    total = as_int(data.get("total"))
    if total is None:
        raise CheckError("invalid_response")
    return {"name": "devices.%s.total" % scope, "count": total,
            "pass": total == EXPECTED_DEVICES[scope]}


def check_stations(base, token, scope):
    data = api(base, list_path("stations", scope=scope), token)
    rows = data.get("list")
    total = as_int(data.get("total"))
    if not isinstance(rows, list) or total is None:
        raise CheckError("invalid_response")
    passed = total == EXPECTED_STATIONS[scope] and len(rows) == EXPECTED_STATIONS[scope]
    if passed:
        for row in rows:
            expected = as_int(row.get("expected_device_count")) if isinstance(row, dict) else None
            registered = as_int(row.get("registered_device_count")) if isinstance(row, dict) else None
            if expected is None or registered is None or expected != registered:
                passed = False
                break
    return {"name": "stations.%s" % scope, "count": total, "pass": passed}


def check_baseline_codes(base, token):
    data = api(base, list_path("devices", scope="baseline", page=1, page_size=100), token)
    rows = data.get("list")
    if not isinstance(rows, list):
        raise CheckError("invalid_response")
    codes = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("device_code"), str):
            raise CheckError("invalid_response")
        codes.add(row["device_code"])
    return {"name": "devices.baseline.codes", "count": len(codes),
            "pass": len(rows) == len(BASELINE_CODES) and codes == BASELINE_CODES}


def check_business_page_two(base, token):
    data = api(base, list_path("devices", scope="business", page=2, page_size=100), token)
    rows = data.get("list")
    if not isinstance(rows, list):
        raise CheckError("invalid_response")
    return {"name": "devices.business.page2", "count": len(rows),
            "pass": len(rows) == BUSINESS_PAGE_TWO}


def check_pv04(base, token):
    data = api(base, list_path("devices", scope="business", station_code="PV-04",
                               page=1, page_size=1), token)
    total = as_int(data.get("total"))
    if total is None:
        raise CheckError("invalid_response")
    return {"name": "devices.business.PV-04", "count": total, "pass": total == PV04_DEVICES}


def check_alarms_business(base, token):
    data = api(base, list_path("alarms", scope="business", page=1, page_size=1), token)
    total = as_int(data.get("total"))
    if total is None or not isinstance(data.get("list"), list):
        raise CheckError("invalid_response")
    return {"name": "alarms.business", "count": total, "pass": True}


def overview_round(base, token):
    snapshot = {}
    for scope in ("all", "business", "baseline"):
        data = api(base, list_path("dashboard/overview", scope=scope), token)
        expected = as_int(data.get("expected_device_count"))
        registered = as_int(data.get("registered_device_count"))
        observed = as_int(data.get("observed_device_count"))
        if expected is None or registered is None or observed is None:
            raise CheckError("invalid_response")
        snapshot[scope] = (expected, registered, observed)
    return snapshot


def overview_passes(snapshot):
    return {scope: (expected == EXPECTED_DEVICES[scope]
                    and registered == EXPECTED_DEVICES[scope]
                    and observed >= EXPECTED_DEVICES[scope])
            for scope, (expected, registered, observed) in snapshot.items()}


def overview_settled(snapshot):
    return all(expected == EXPECTED_DEVICES[scope] and registered == EXPECTED_DEVICES[scope]
               for scope, (expected, registered, observed) in snapshot.items())


def check_overviews(base, token):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        snapshot = overview_round(base, token)
        if overview_settled(snapshot) and all(overview_passes(snapshot).values()):
            break
        remaining = deadline - time.monotonic()
        if not overview_settled(snapshot) or remaining <= 0:
            break
        time.sleep(min(WAIT_INTERVAL_SECONDS, remaining))
    passed = overview_passes(snapshot)
    return [{"name": "overview.%s" % scope, "count": snapshot[scope][2], "pass": passed[scope]}
            for scope in ("all", "business", "baseline")]


def read_password(path):
    try:
        password = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise CheckError("password_file_unreadable") from None
    if not password:
        raise CheckError("password_file_empty")
    return password


def write_output(path, checks):
    payload = {"checks": checks, "result": "pass" if all(c["pass"] for c in checks) else "fail"}
    try:
        with path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        raise CheckError("output_exists") from None
    except OSError:
        raise CheckError("output_unwritable") from None


def run(base, password):
    token = api(base, "/api/v1/auth/login",
                payload={"username": "demo-operator", "password": password}).get("token")
    if not isinstance(token, str) or not token:
        raise CheckError("login_response_invalid")
    checks = [{"name": "login", "count": 1, "pass": True}]
    for scope in ("all", "business", "baseline"):
        checks.append(check_device_total(base, token, scope))
    for scope in ("all", "business", "baseline"):
        checks.append(check_stations(base, token, scope))
    checks.append(check_baseline_codes(base, token))
    checks.append(check_business_page_two(base, token))
    checks.append(check_pv04(base, token))
    checks.append(check_alarms_business(base, token))
    checks.extend(check_overviews(base, token))
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:18080")
    parser.add_argument("--password-file", type=Path, default=Path("/etc/powersystem/demo-password"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    try:
        checks = run(base, read_password(args.password_file))
        if args.output is not None:
            write_output(args.output, checks)
    except CheckError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    for check in checks:
        print("%-26s %6d  %s" % (check["name"], check["count"],
                                 "pass" if check["pass"] else "fail"))
    print("result=%s" % ("pass" if all(c["pass"] for c in checks) else "fail"))
    return 0 if all(c["pass"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
