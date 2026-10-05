#!/usr/bin/env bash
# Actual M3 no-search rollback and restore. Run only after real requests finish.
set -euo pipefail
root=${POWERSYSTEM_DIR:-/opt/powersystem}
release=${1:?absolute Bocha release directory required}
case "$release" in "$root"/runtime/bocha-*) ;; *) exit 2 ;; esac
cd "$root"
test -s "$release/env-baseline"
cp .env "$release/env-bocha"
chmod 600 "$release/env-bocha"
python3 - "$release" <<'PY'
import pathlib,subprocess,json,sys
r=pathlib.Path(sys.argv[1]); images={}
for service in ('agent','frontend'):
    image=subprocess.check_output(['docker','inspect','--format','{{.Image}}','powersystem-stage4-'+service+'-1'],text=True).strip()
    tag='powersystem-bocha-current-'+service+':'+r.name
    subprocess.run(['docker','tag',image,tag],check=True);images[service]=tag
(r/'current-images.json').write_text(json.dumps(images))
def override(path, mapping):
    path.write_text('services:\n'+''.join('  '+s+':\n    image: '+tag+'\n' for s,tag in mapping.items()))
override(r/'rollback.yaml',dict(line.split() for line in (r/'baseline-images.txt').read_text().splitlines()))
override(r/'restore.yaml',images)
PY
base=(docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml -f stages/07-agent/deploy/agent.compose.yaml)
# Ensure any failure restores the verified current release, using images already built.
restore() {
  cp "$release/env-bocha" .env
  "${base[@]}" -f stages/07-agent/deploy/search.compose.yaml -f "$release/restore.yaml" --profile agent up -d --no-deps --no-build --wait agent frontend
}
trap restore EXIT
cp "$release/env-baseline" .env
"${base[@]}" -f "$release/rollback.yaml" --profile agent up -d --no-deps --no-build --wait agent frontend
docker exec powersystem-stage4-agent-1 python -c 'import urllib.request,json; d=json.load(urllib.request.urlopen("http://127.0.0.1:8092/health")); assert not d["search_configured"]; print("rollback_search_disabled=verified")'
restore
trap - EXIT
docker exec powersystem-stage4-agent-1 python -c 'import urllib.request,json; d=json.load(urllib.request.urlopen("http://127.0.0.1:8092/health")); assert d["search_configured"]; print("restored_search_configured=verified")'
docker exec -i powersystem-stage4-agent-1 python - <<'PY'
import asyncio,json
from dataclasses import replace
import httpx,redis.asyncio as redis
from agent.config import Config
from agent.search import Search
async def main():
    cfg=Config.load();r=redis.from_url(cfg.redis_url)
    before=await r.get('stage7:model:pause')
    async with httpx.AsyncClient() as client:
        status,results=await Search(replace(cfg,search_key='invalid-test-only-key'),client).run('风力发电机 覆冰 官方文档 排查')
    assert status=='auth_failed' and not results
    assert before==await r.get('stage7:model:pause')
    await r.aclose()
    print('real_search_auth_failure_isolated=verified')
asyncio.run(main())
PY
printf 'rollback_and_restore=verified\n'
