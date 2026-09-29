#!/bin/sh
set -eu
gateway=powersystem-m4-load-gateway-1
postgres=powersystem-m4-load-postgres-1
before=$(docker exec "$postgres" psql -U powersystem -d powersystem -tAc 'SELECT count(*) FROM telemetry_inbox')
docker stop "$gateway" >/dev/null
trap 'docker start "$gateway" >/dev/null 2>&1 || true' EXIT
docker run --rm --name powersystem-m4-load-generator \
  --network powersystem-m4-load_default --cpuset-cpus 54,55 --memory 256m \
  -v /tmp/m4-load-200.py:/load.py:ro --entrypoint python \
  powersystem-stage4-simulator:latest /load.py --broker emqx \
  --cycles 6 --run-id "m4-recovery-$(date -u +%Y%m%dT%H%M%SZ)"
started=$(date +%s)
docker start "$gateway" >/dev/null
target=$((before + 1200))
while :; do
  total=$(docker exec "$postgres" psql -U powersystem -d powersystem -tAc 'SELECT count(*) FROM telemetry_inbox')
  pending=$(docker exec "$postgres" psql -U powersystem -d powersystem -tAc 'SELECT count(*) FROM telemetry_inbox WHERE processed_at IS NULL')
  elapsed=$(($(date +%s) - started))
  if [ "$total" -ge "$target" ] && [ "$pending" -eq 0 ]; then
    printf 'recovery_elapsed_s=%s total=%s target=%s pending=%s\n' "$elapsed" "$total" "$target" "$pending"
    break
  fi
  if [ "$elapsed" -ge 30 ]; then
    printf 'RECOVERY_FAILED elapsed_s=%s total=%s target=%s pending=%s\n' "$elapsed" "$total" "$target" "$pending" >&2
    exit 1
  fi
  sleep 1
done
trap - EXIT
