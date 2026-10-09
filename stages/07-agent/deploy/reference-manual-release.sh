#!/usr/bin/env bash
# Reviewable SUN2000 reference-manual release. The PDF is installed separately.
set -euo pipefail
root=${POWERSYSTEM_DIR:-/opt/powersystem}
archive=${1:?reviewed archive required}
expected=${2:?SHA256 required}
test "$(sha256sum "$archive" | cut -d' ' -f1)" = "$expected"
cd "$root"
release="$root/runtime/reference-manual-$(date -u +%Y%m%dT%H%M%SZ)"
release_id=${release##*/}
mkdir -m 700 "$release"
cp "$archive" "$release/approved.tar.gz"
for service in api agent frontend; do
  docker inspect --format '{{.Image}}' "powersystem-stage4-$service-1" > "$release/baseline-$service-image.txt"
  docker tag "$(cat "$release/baseline-$service-image.txt")" "powersystem-reference-manual-$service:baseline-$release_id"
done
python3 - "$release" <<'PY'
import pathlib,sys
release=pathlib.Path(sys.argv[1])
images={service:'powersystem-reference-manual-'+service+':baseline-'+release.name for service in ('api','agent','frontend')}
(release/'baseline.yaml').write_text('services:\n'+''.join('  '+key+':\n    image: '+value+'\n' for key,value in images.items()))
PY
rollback_release() {
  local failed=$?
  if [ -s "$release/manifest.json" ]; then
    python3 - "$root" "$release" <<'PY'
import pathlib,tarfile,json,hashlib,sys
root,r=map(pathlib.Path,sys.argv[1:])
with tarfile.open(r/'source-baseline.tar.gz') as archive:
    for row in json.loads((r/'manifest.json').read_text()):
        target=(root/row['path']).resolve()
        assert target.is_relative_to(root.resolve())
        if row['existed']:
            raw=archive.extractfile(row['path']).read()
            assert hashlib.sha256(raw).hexdigest()==row['baseline_sha256']
            target.write_bytes(raw)
        elif target.is_file() and not target.is_symlink(): target.unlink()
PY
    docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml -f stages/07-agent/deploy/search.compose.yaml -f "$release/baseline.yaml" --profile agent up -d --no-deps --no-build --pull never --force-recreate --wait api agent frontend
    docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml -f stages/07-agent/deploy/search.compose.yaml --profile agent restart gateway
  fi
  printf 'release_failed_and_rollback_attempted=%s\n' "$failed" >&2
  exit "$failed"
}
trap rollback_release ERR
python3 - "$root" "$release" <<'PY'
import pathlib,tarfile,json,hashlib,sys
root,release=map(pathlib.Path,sys.argv[1:]); rows=[]
with tarfile.open(release/'approved.tar.gz') as package:
    members=package.getmembers()
    if len({m.name for m in members}) != len(members): raise ValueError('duplicate release members')
    for member in members:
        path=pathlib.PurePosixPath(member.name); target=(root/member.name).resolve()
        allowed=(member.name.startswith(('stages/07-agent/','stages/04-frontend/src/'))
                 or member.name in ('stages/02-backend/deploy/postgres/001_init.sql',
                                    'stages/02-backend/internal/api/handlers.go',
                                    'stages/02-backend/internal/model/model.go',
                                    'stages/02-backend/internal/store/store.go',
                                    'stages/04-frontend/deploy/backup.sh',
                                    'stages/04-frontend/deploy/restore-verify.sh')
                 or (member.name.startswith('设备知识/') and path.suffix in ('.md','.json')))
        if not allowed or path.is_absolute() or '..' in path.parts or not member.isfile() or not target.is_relative_to(root.resolve()) or any(part in ('.env','.git','runtime','artifacts','node_modules','__pycache__') for part in path.parts):
            raise ValueError('invalid release member')
    with tarfile.open(release/'source-baseline.tar.gz','w:gz') as baseline:
        for member in members:
            target=root/member.name; existed=target.is_file()
            row={'path':member.name,'existed':existed}
            if existed:
                row['baseline_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
                baseline.add(target,arcname=member.name)
            raw=package.extractfile(member).read(); row['sha256']=hashlib.sha256(raw).hexdigest(); rows.append(row)
    (release/'manifest.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
    for member in members:
        target=root/member.name; target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(package.extractfile(member).read())
        target.chmod(0o644)
PY
dc=(docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml -f stages/07-agent/deploy/search.compose.yaml --profile agent)
"${dc[@]}" config --quiet
"${dc[@]}" build api agent frontend > "$release/build.log" 2>&1
"${dc[@]}" run --rm --no-deps --entrypoint python agent -c 'import agent.main; print("candidate_import_ok")' >> "$release/build.log" 2>&1
for service in api agent frontend; do
  docker tag "powersystem-stage4-$service" "powersystem-reference-manual-$service:current-$release_id"
done
python3 - "$release" <<'PY'
import pathlib,sys
r=pathlib.Path(sys.argv[1])
(r/'current.yaml').write_text('services:\n'+''.join('  '+s+':\n    image: powersystem-reference-manual-'+s+':current-'+r.name+'\n' for s in ('api','agent','frontend')))
PY
"${dc[@]}" -f "$release/current.yaml" up -d --no-deps --no-build --pull never --force-recreate --wait api agent frontend
# ALTER TABLE devices invalidates prepared SELECT plans in the long-running gateway.
# Reopen its PostgreSQL connection after the API has applied the schema migration.
"${dc[@]}" restart gateway
python3 - "$release" <<'PY'
import pathlib,subprocess,json,sys
r=pathlib.Path(sys.argv[1]); images={}
for service in ('api','agent','frontend'):
    images[service]=subprocess.check_output(['docker','inspect','--format','{{.Image}}','powersystem-stage4-'+service+'-1'],text=True).strip()
(r/'current-images.json').write_text(json.dumps(images,indent=2))
assert len(images)==3
PY
printf '%s\n' "$release"
trap - ERR
