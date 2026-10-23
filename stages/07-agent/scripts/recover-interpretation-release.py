"""Resume only the verified rollback's registry/gate tail; no gate is skipped."""
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path('/opt/powersystem')
STATE = ROOT/'runtime/production-stack'
BATCH = ROOT/'runtime/interpretation-official-20261021-r3'


def main():
    assert socket.gethostname() == 'R730XD' and os.geteuid() == 0
    os.umask(0o077)
    sys.path.insert(0, str(ROOT/'stages/09-ops-agent'))
    from ops_contract import digest
    spec = importlib.util.spec_from_file_location('controller', ROOT/'stages/04-frontend/deploy/production-stack.py')
    ctrl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ctrl)
    with (STATE/'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = ctrl.read(STATE/'pending.json')
        assert pending['candidate'] == BATCH.name
        previous = ctrl.release(pending['previous'])
        manifest = ctrl.immutable(previous)
        rows = ctrl.inspect(); ctrl.healthy(rows); ctrl.drain()
        assert set(rows) == set(pending['before'])
        assert all(row['Image'] == manifest['images'][service] for service, row in rows.items())
        # Only reconcile provisional metadata for the exact release's services.
        registry_path = Path('/etc/powersystem-ops-read/registry.json')
        registry = ctrl.read(registry_path)
        query = "SELECT coalesce(json_object_agg(asset_id,version),'{}'::json)::text FROM ma_assets"
        retained = json.loads(ctrl.command(['docker','exec',ctrl.PROJECT+'-postgres-1','psql','-U','powersystem','-d','ma_ops','-Atqc',query]))
        changed = []
        before = registry_path.read_bytes()
        for asset in registry['assets']:
            committed = retained[asset['asset_id']]
            assert asset['version'] >= committed
            if asset['version'] <= committed + 1:
                continue
            identity = registry['private_assets'][asset['asset_id']]['identity']
            service = identity['service']
            assert service in pending['services']
            row = rows[service]
            actual = {'container_id':row['Id'], 'image_id':row['Image'],
                      'project':row['Config']['Labels']['com.docker.compose.project'], 'service':service}
            assert identity == actual and asset['identity_hash'] == digest(actual)
            changed.append({'asset':asset['asset_id'], 'provisional_before':asset['version'], 'committed':committed, 'retry_version':committed+1})
            asset['version'] = committed+1
        assert changed
        stamp = str(__import__('time').time_ns())
        backup = BATCH/('registry-before-recovery-'+stamp+'.json')
        backup.write_bytes(before); backup.chmod(0o600)
        ctrl.write(registry_path, registry)
        # Existing helper reloads executor and imports through Store.import_assets.
        # Run the complete immutable release gate, including HTTPS/WSS acceptance.
        ctrl.refresh_registry(previous, manifest['origin_environment'])
        accepted = ctrl.gate(previous, pending['services'], pending['before'])
        ctrl.set_active(previous, accepted)
        proof = {'passed':True, 'kind':'resume_registry_and_full_rollback_gate',
                 'transaction':pending, 'reconciled':changed,
                 'registry_backup_sha256':hashlib.sha256(before).hexdigest(),
                 'restored_at':dt.datetime.now(dt.timezone.utc).isoformat(), 'gates_skipped':False}
        ctrl.write(STATE/('rollback-'+stamp+'.json'), proof)
        ctrl.write(BATCH/'registry-recovery.json', proof)
        (STATE/'pending.json').unlink()
        print(json.dumps({'rollback_completed':True,'reconciled':changed,'gates_skipped':False}),flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        import traceback
        print(json.dumps({'completed':False,'error_type':type(error).__name__,
                          'frames':[{'file':Path(t.filename).name,'line':t.lineno,'function':t.name}
                                    for t in traceback.extract_tb(error.__traceback__)]}),flush=True)
        raise SystemExit(1)
