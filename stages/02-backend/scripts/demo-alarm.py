#!/usr/bin/env python3
"""Server-local demo lifecycle using the running API binary and normal workers.

create: new manual API interpretation, idempotent with --request-id.
set-state: acked (requires --actor), recovered; unhandled creates a NEW alarm.
status: reads status/result without invoking a model. Never embeds credentials.
"""
import argparse
import importlib.util
import json
import re
from pathlib import Path
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/opt/powersystem', help='registered application root')
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create', help='create a manual demo and request actual API interpretation')
    create.add_argument('--device', default='INV-1001')
    create.add_argument('--level', choices=('minor', 'major', 'urgent'), default='major')
    create.add_argument('--analysis', choices=('model','fixed'), default='model', help='model calls the preserved API; fixed is a zero-model demonstration')
    create.add_argument('--request-id', default=None, help='reuse this key when retrying the same creation')
    state = sub.add_parser('set-state', help='change a manual demo through the business lifecycle')
    state.add_argument('--alarm-id', type=int)
    state.add_argument('--state', required=True, choices=('unhandled', 'acked', 'recovered'))
    state.add_argument('--actor', help='existing admin/operator username; required for acked')
    state.add_argument('--device', default='INV-1001')
    state.add_argument('--level', choices=('minor', 'major', 'urgent'), default='major')
    state.add_argument('--analysis', choices=('model','fixed'), default='model')
    state.add_argument('--request-id')
    status = sub.add_parser('status', help='read the real interpretation result and model call count')
    status.add_argument('--alarm-id', required=True, type=int)
    args = parser.parse_args()
    mode = args.command
    if mode == 'set-state':
        if args.state == 'unhandled':
            if args.alarm_id: parser.error('unhandled starts a new alarm: omit --alarm-id; old acknowledgements/recovery remain recorded')
            mode = 'create'
        else:
            if not args.alarm_id or args.alarm_id <= 0: parser.error('positive --alarm-id is required')
            mode = 'ack' if args.state == 'acked' else 'recover'
            if mode == 'ack' and not args.actor: parser.error('--actor is required for acked')
    if mode == 'status' and args.alarm_id <= 0: parser.error('positive --alarm-id is required')
    root = Path(args.root).resolve()
    controller = root / 'stages/04-frontend/deploy/production-stack.py'
    spec = importlib.util.spec_from_file_location('demo_production_stack', controller)
    ps = importlib.util.module_from_spec(spec);sys.modules[spec.name] = ps;spec.loader.exec_module(ps)
    ps.registered_business_host('release')
    candidate, manifest = ps.active();ps.current_matches(candidate, manifest)
    rows = ps.inspect();row = rows['api']
    if not row['State']['Running'] or row['Image'] != manifest['images']['api']:
        raise RuntimeError('accepted_api_not_running')
    command = ['docker', 'exec', row['Id'], '/app/server', '-demo-alarm', mode]
    request_id = None
    if mode == 'create':
        request_id = args.request_id or uuid.uuid4().hex
        # Print before executing: an interrupted terminal can safely retry the key.
        print(json.dumps({'request_id': request_id, 'interpretation_mode': args.analysis}), flush=True)
        command += ['-demo-request-id', request_id, '-demo-device', args.device, '-demo-level', args.level, '-demo-analysis', args.analysis]
    else:
        command += ['-demo-alarm-id', str(args.alarm_id)]
        if mode == 'ack': command += ['-demo-actor', args.actor]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    if result.returncode:
        active = re.search(r'demo_already_active:(\d+)', result.stderr)
        if active:
            raise RuntimeError('active demo alarm #' + active.group(1) + ': query it or set-state recovered before a new demonstration; retrying the original request-id reuses it')
        # The operation can have committed before a lost reply; its retry key
        # remains the authority. Never issue an automatic second creation.
        raise RuntimeError('demo_operation_failed; retain request_id and retry the same operation; check API configuration and server logs')
    payloads = []
    for line in result.stdout.splitlines():
        try: value = json.loads(line)
        except json.JSONDecodeError: continue
        if isinstance(value, dict) and value.get('operation') == mode and 'alarm_id' in value: payloads.append(value)
    if len(payloads) != 1: raise RuntimeError('demo_reply_invalid; retain request_id before retrying')
    out = payloads[0]
    if request_id: out['request_id'] = request_id
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    try: main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr);raise SystemExit(1)
