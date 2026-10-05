#!/usr/bin/env bash
# Reviewed incremental package; preserve the current M3 no-search rollback state.
set -euo pipefail
root=${POWERSYSTEM_DIR:-/opt/powersystem}
archive=${1:?reviewed archive required}
expected=${2:?SHA256 required}
test "$(sha256sum "$archive" | cut -d' ' -f1)" = "$expected"
cd "$root"
release="$root/runtime/bocha-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -m 700 "$release"
cp .env "$release/env-baseline"
chmod 600 "$release/env-baseline"
for service in agent frontend; do
  image=$(docker inspect --format '{{.Image}}' "powersystem-stage4-$service-1")
  tag="powersystem-bocha-baseline-$service:$(basename "$release")"
  docker tag "$image" "$tag"
  printf '%s %s\n' "$service" "$tag" >> "$release/baseline-images.txt"
done
python3 - "$archive" "$root" "$release" <<'PY'
import pathlib,sys,tarfile,json,hashlib
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
    manifest=[]
    for member in members:
        target=(root/member.name).resolve(); target.parent.mkdir(parents=True,exist_ok=True)
        raw=package.extractfile(member).read();target.write_bytes(raw)
        manifest.append({'path':member.name,'sha256':hashlib.sha256(raw).hexdigest()})
    (release/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
env=root/'.env'; text=env.read_text()
values={'AGENT_SEARCH_PROVIDER':'bocha','AGENT_SEARCH_URL':'https://api.bocha.cn/v1/web-search','AGENT_SEARCH_KEY_FILE':'/etc/powersystem/agent-search-key'}
lines=[line for line in text.splitlines() if not any(line.startswith(k+'=') for k in values)]
env.write_text('\n'.join(lines+[k+'='+v for k,v in values.items()])+'\n')
PY
test -s /etc/powersystem/agent-search-key
chown 10001:10001 /etc/powersystem/agent-search-key
chmod 600 /etc/powersystem/agent-search-key
dc=(docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml -f stages/07-agent/deploy/search.compose.yaml --profile agent)
"${dc[@]}" config --quiet
"${dc[@]}" build agent frontend > "$release/build.log" 2>&1
"${dc[@]}" up -d --no-deps --wait agent frontend
printf '%s\n' "$release"
