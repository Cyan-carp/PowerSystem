#!/usr/bin/env bash
# Incremental release from a reviewed archive; leaves private config and data volumes intact.
set -euo pipefail
project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
archive=${1:?usage: v2m3-release.sh reviewed.tar.gz sha256}
expected=${2:?archive SHA256 required}
test -d "$project_dir/stages/07-agent"
test "$(sha256sum "$archive" | cut -d' ' -f1)" = "$expected"
release="$project_dir/runtime/v2m3-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$release"
python3 - "$archive" "$project_dir" "$release" <<'PY'
import pathlib,sys,tarfile,json
archive,root,release=map(pathlib.Path,sys.argv[1:])
with tarfile.open(archive) as package:
    members=package.getmembers()
    for member in members:
        target=(root/member.name).resolve()
        if not member.isfile() or not target.is_relative_to(root.resolve()) or '.env' in pathlib.Path(member.name).parts:
            raise ValueError('invalid release member')
    with tarfile.open(release/'source-baseline.tar.gz','w:gz') as baseline:
        for member in members:
            target=root/member.name
            if target.is_file(): baseline.add(target,arcname=member.name)
    (release/'manifest.json').write_text(json.dumps([m.name for m in members],indent=2))
    # Python on the host predates tarfile's filter argument. All members were
    # verified as regular files inside root above; write files without link extraction.
    for member in members:
        target=(root/member.name).resolve()
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(package.extractfile(member).read())
PY
cd "$project_dir"
dc=(docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml --profile agent)
for service in agent api frontend; do
  image=$(docker inspect --format '{{.Image}}' "powersystem-stage4-$service-1")
  tag="powersystem-v2m3-baseline-$service:$(basename "$release")"
  docker tag "$image" "$tag"
  printf '%s %s\n' "$service" "$tag" >> "$release/baseline-images.txt"
done
mkdir -p runtime/agent-knowledge-misses
chown 10001:10001 runtime/agent-knowledge-misses
chmod 700 runtime/agent-knowledge-misses
"${dc[@]}" config --quiet
"${dc[@]}" build api frontend agent > "$release/build.log" 2>&1
"${dc[@]}" up -d --no-deps --wait api frontend agent
printf '%s\n' "$release"
