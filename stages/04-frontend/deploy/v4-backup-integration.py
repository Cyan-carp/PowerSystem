#!/usr/bin/env python3
"""Minimal docker-inspect glue for V4 backup/restore orchestration.

Only container state, environment switches and the /artifacts bind are read.
Row bodies, DSNs and secrets never pass through this tool. Every failure is a
fixed code so shell callers can fail closed without printing private values.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL = "v4-backup-integration"


class Fail(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def read_inspect(path):
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        value = json.loads(text)
    except ValueError:
        raise Fail("inspect_invalid_json")
    if isinstance(value, list):
        if not value:
            return None
        value = value[0]
    if not isinstance(value, dict):
        raise Fail("inspect_invalid")
    return value


def env_map(data):
    result = {}
    for item in (data or {}).get("Config", {}).get("Env", []) or []:
        if isinstance(item, str) and "=" in item:
            key, value = item.split("=", 1)
            result[key] = value
    return result


def flag(env, name):
    return str(env.get(name, "")).strip().lower() in ("true", "1")


def v4_active(env):
    return flag(env, "V4_RUNS_ENABLED") or flag(env, "V4_ARTIFACTS_ENABLED")


def artifact_bind(data, project_dir):
    """The single /artifacts bind must live under project_dir/runtime, unsymlinked."""
    mounts = [item for item in (data or {}).get("Mounts", []) or []
              if isinstance(item, dict) and item.get("Destination") == "/artifacts"]
    if not mounts:
        raise Fail("artifacts_bind_missing")
    if len(mounts) != 1:
        raise Fail("artifacts_bind_ambiguous")
    mount = mounts[0]
    if mount.get("Type") != "bind":
        raise Fail("artifacts_bind_not_bind")
    source = mount.get("Source")
    if not isinstance(source, str) or not os.path.isabs(source):
        raise Fail("artifacts_bind_source_invalid")
    runtime = os.path.realpath(os.path.join(project_dir, "runtime"))
    normalized = os.path.normpath(source)
    real = os.path.realpath(source)
    if os.path.islink(source) or real != normalized:
        raise Fail("artifacts_bind_symlink")
    if not (real == runtime or real.startswith(runtime + os.sep)):
        raise Fail("artifacts_bind_outside_runtime")
    if not os.path.isdir(real):
        raise Fail("artifacts_bind_not_directory")
    return real


def writer(data, fallback):
    name = (data or {}).get("Name") or fallback
    return {"container": str(name).lstrip("/") if name else "",
            "id": (data or {}).get('Id'),
            "running": (data or {}).get("State", {}).get("Running") is True}


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def now_utc():
    return datetime.now(timezone.utc)


def build_state(rag, ops):
    return {"tool": TOOL, "rag": writer(rag, ""), "ops": writer(ops, "")}


def build_evidence(rag_after, ops_after, state, scope, run_id, now, ttl_hours):
    """Evidence is only emitted when both writers are actually stopped."""
    writers = {}
    for label, after in (("rag", rag_after), ("ops", ops_after)):
        original = (state or {}).get(label)
        if not isinstance(original, dict) or not isinstance(original.get("running"), bool):
            raise Fail("writer_state_invalid")
        current = writer(after, "")
        if original.get('container') and (current['container']!=original['container'] or not original.get('id') or current['id']!=original['id']):
            raise Fail(label + '_identity_changed')
        if current["running"]:
            raise Fail(label + "_still_running")
        writers[label] = {"container": original.get("container") or current["container"],
                          "was_running": original["running"], "running": False}
    return {"writes_paused": True, "scope": scope, "paused_at": iso(now),
            "expires_at": iso(now + timedelta(hours=ttl_hours)),
            "run_id": run_id, "writers": writers}


def resume_names(state):
    names = []
    for label in ("ops", "rag"):
        item = (state or {}).get(label) or {}
        if item.get("running") is True and item.get("container"):
            names.append(item["container"])
    return names


def write_new(path, value):
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    target = Path(path)
    try:
        with open(target, "x", encoding="utf-8") as stream:
            stream.write(text)
    except FileExistsError:
        raise Fail("output_exists")
    except OSError:
        raise Fail("output_unwritable")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def read_state(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise Fail("state_unreadable")
    except ValueError:
        raise Fail("state_invalid")
    if not isinstance(value, dict):
        raise Fail("state_invalid")
    return value


def run(args):
    if args.command == 'compatible':
        rag=env_map(read_inspect(args.rag_inspect));ops=env_map(read_inspect(args.ops_inspect))
        if any(flag(ops,k) for k in ('V4_ACTIONS_ENABLED','V4_OPS_WRITE_ENABLED','V4_SCHEDULES_ENABLED')) and not v4_active(rag):
            raise Fail('v4_writer_configuration_inconsistent')
        return {'tool':TOOL,'configuration':'compatible'}
    if args.command == "probe":
        data = read_inspect(args.inspect)
        env = env_map(data)
        active = v4_active(env)
        root = ""
        if active:
            if data is None:
                raise Fail("v4_container_missing")
            root = artifact_bind(data, args.project_dir)
        current = writer(data, "")
        return {"tool": TOOL, "active": active,
                "runs": flag(env, "V4_RUNS_ENABLED"),
                "artifacts": flag(env, "V4_ARTIFACTS_ENABLED"),
                "container": current["container"], "running": current["running"],
                "artifact_root": root}
    if args.command == "state":
        value = build_state(read_inspect(args.rag_inspect), read_inspect(args.ops_inspect))
        write_new(args.output, value)
        return {"tool": TOOL, "state": "written", "rag_running": value["rag"]["running"],
                "ops_running": value["ops"]["running"]}
    if args.command == "evidence":
        state = read_state(args.state)
        value = build_evidence(read_inspect(args.rag_inspect), read_inspect(args.ops_inspect),
                               state, args.scope, args.run_id, now_utc(), args.ttl_hours)
        write_new(args.output, value)
        return {"tool": TOOL, "evidence": "written", "scope": value["scope"]}
    if args.command == "resume":
        return None  # names go to stdout as plain lines
    raise Fail("command_invalid")


def main(argv=None):
    parser = argparse.ArgumentParser(prog=TOOL)
    sub = parser.add_subparsers(dest="command", required=True)
    compatible=sub.add_parser('compatible')
    compatible.add_argument('--rag-inspect',required=True)
    compatible.add_argument('--ops-inspect')
    probe = sub.add_parser("probe")
    probe.add_argument("--inspect", required=True)
    probe.add_argument("--project-dir", required=True)
    state = sub.add_parser("state")
    state.add_argument("--rag-inspect", required=True)
    state.add_argument("--ops-inspect")
    state.add_argument("--output", required=True)
    evidence = sub.add_parser("evidence")
    evidence.add_argument("--rag-inspect", required=True)
    evidence.add_argument("--ops-inspect")
    evidence.add_argument("--state", required=True)
    evidence.add_argument("--output", required=True)
    evidence.add_argument("--scope", choices=("live", "restore"), default="live")
    evidence.add_argument("--run-id", required=True)
    evidence.add_argument("--ttl-hours", type=float, default=1)
    resume = sub.add_parser("resume")
    resume.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "resume":
            for name in resume_names(read_state(args.state)):
                sys.stdout.write(name + "\n")
            return 0
        result = run(args)
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    except Fail as error:
        sys.stdout.write(json.dumps({"tool": TOOL, "status": "failed",
                                     "error": {"code": error.code}}, sort_keys=True) + "\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
