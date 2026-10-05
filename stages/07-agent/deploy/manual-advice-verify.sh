#!/usr/bin/env bash
# Serialized controlled outage and actual source/image rollback, always restoring.
set -euo pipefail
root=${POWERSYSTEM_DIR:-/opt/powersystem}
release=${1:?absolute release directory required}
case "$release" in "$root"/runtime/manual-advice-*) ;; *) exit 2 ;; esac
cd "$root"
test -s "$release/current.yaml"
dc=(docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml -f stages/07-agent/deploy/search.compose.yaml --profile agent)
sources() {
  python3 - "$root" "$release" "$1" <<'PY'
import pathlib,tarfile,json,hashlib,sys
root,r=map(pathlib.Path,sys.argv[1:3]); mode=sys.argv[3]
rows=json.loads((r/'manifest.json').read_text()); archive=r/('approved.tar.gz' if mode=='current' else 'source-baseline.tar.gz')
with tarfile.open(archive) as package:
    for row in rows:
        target=(root/row['path']).resolve()
        if not target.is_relative_to(root.resolve()): raise ValueError('path outside project')
        if mode=='baseline' and not row['existed']:
            if target.is_file() and not target.is_symlink(): target.unlink()
            continue
        raw=package.extractfile(row['path']).read()
        assert hashlib.sha256(raw).hexdigest()==row['sha256' if mode=='current' else 'baseline_sha256']
        target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(raw)
        if mode=='current': target.chmod(0o644)
print('sources_'+mode+'=matched')
PY
}
restore_current() {
  sources current
  "${dc[@]}" -f "$release/current.yaml" up -d --no-deps --no-build --pull never --force-recreate --wait agent frontend
}
main() {
  trap restore_current EXIT
  "${dc[@]}" stop agent
  python3 stages/07-agent/deploy/acceptance-v2m3.py --fault-check --output "$release/fault-agent-down"
  restore_current
  sources baseline
  "${dc[@]}" -f "$release/baseline.yaml" up -d --no-deps --no-build --pull never --force-recreate --wait agent frontend
  docker inspect --format '{{.Image}}|{{.State.Health.Status}}' powersystem-stage4-agent-1 powersystem-stage4-frontend-1 > "$release/rollback-images.txt"
  restore_current
  docker inspect --format '{{.Image}}|{{.State.Health.Status}}' powersystem-stage4-agent-1 powersystem-stage4-frontend-1 > "$release/restored-images.txt"
  trap - EXIT
  printf 'fault_isolation_and_actual_source_image_rollback=verified\n'
}
main
