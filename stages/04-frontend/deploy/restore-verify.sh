#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: restore-verify.sh /var/backups/powersystem/powersystem-...tar.gz.gpg" >&2
  exit 2
fi

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
env_file="$project_dir/.env"
compose_file="$project_dir/stages/04-frontend/deploy/restore-compose.yaml"
backup_dir=${POWERSYSTEM_BACKUP_DIR:-/var/backups/powersystem}
passphrase_file=${POWERSYSTEM_BACKUP_PASSPHRASE_FILE:-/etc/powersystem/backup-passphrase}
archive=$(realpath -- "$1")
case "$archive" in
  "$backup_dir"/powersystem-*.tar.gz.gpg) ;;
  *) echo "archive must be a PowerSystem backup in $backup_dir" >&2; exit 2 ;;
esac
test -r "$archive"
run_id=$(basename "$archive" .tar.gz.gpg)
restore_root="$backup_dir/restore-$run_id"
mkdir -m 700 "$restore_root"
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$passphrase_file" --decrypt --output "$restore_root/archive.tar.gz" "$archive"
tar -C "$restore_root" -xzf "$restore_root/archive.tar.gz"
(cd "$restore_root/$run_id" && sha256sum -c SHA256SUMS)
test -s "$restore_root/$run_id/config/.env"
test -s "$restore_root/$run_id/config/feishu-webhook-url"
test -s "$restore_root/$run_id/config/feishu-sign-secret"
test -s "$restore_root/$run_id/config/business-notify-token"
test -s "$restore_root/$run_id/model.json"
test -s "$restore_root/$run_id/metadata.json"
if [ -f "$restore_root/$run_id/knowledge-index.json" ]; then
  python3 -c 'import json,hashlib,sys; d=json.load(open(sys.argv[1])); assert d["version"] == hashlib.sha256(json.dumps(d["chunks"],ensure_ascii=False,sort_keys=True).encode()).hexdigest(); print("knowledge_index_hash=matched")' "$restore_root/$run_id/knowledge-index.json"
  if [ -d "$restore_root/$run_id/agent-knowledge-misses" ]; then
    python3 -c 'import json,pathlib,sys; n=0; root=pathlib.Path(sys.argv[1]); files=list(root.glob("misses-*.jsonl")); [(json.loads(line)) for p in files for line in p.read_text().splitlines()]; print("knowledge_miss_files="+str(len(files)))' "$restore_root/$run_id/agent-knowledge-misses"
  fi
fi
project_name="powersystem-restore-$(date -u +%s)"
export POWERSYSTEM_BACKUP_DIR="$backup_dir"
dc=(docker compose --project-name "$project_name" --env-file "$env_file" -f "$compose_file")
"${dc[@]}" up -d --wait
pg_container=$("${dc[@]}" ps -q postgres)
docker cp "$restore_root/$run_id/postgres.dump" "$pg_container:/tmp/postgres.dump"
"${dc[@]}" exec -T postgres pg_restore -U powersystem -d powersystem /tmp/postgres.dump
"${dc[@]}" exec -T tdengine sh -c 'taosdump -p"$TAOS_ROOT_PASSWORD" -i "$1"' sh "/backup/restore-$run_id/$run_id/tdengine"
printf 'isolated_project=%s\n' "$project_name"
printf 'postgres_inbox_rows='
"${dc[@]}" exec -T postgres psql -U powersystem -d powersystem -Atqc 'SELECT COUNT(*) FROM telemetry_inbox'
business_outbox_present=$("${dc[@]}" exec -T postgres psql -U powersystem -d powersystem -Atqc "SELECT to_regclass('public.business_notification_outbox') IS NOT NULL")
test "$business_outbox_present" = t
printf 'business_outbox_present=%s\n' "$business_outbox_present"
if [ -f "$restore_root/$run_id/redis.rdb" ]; then
  for agent_file in agent-service-token agent-monitor-token agent-model-key; do
    test -f "$restore_root/$run_id/config/agent/$agent_file"
  done
  agent_tables_present=$("${dc[@]}" exec -T postgres psql -U powersystem -d powersystem -Atqc "SELECT to_regclass('public.agent_interpretations') IS NOT NULL AND to_regclass('public.agent_monitor_events') IS NOT NULL")
  test "$agent_tables_present" = t
  redis_container=$("${dc[@]}" ps -q redis)
  "${dc[@]}" stop redis
  docker cp "$restore_root/$run_id/redis.rdb" "$redis_container:/data/dump.rdb"
  "${dc[@]}" start redis
  for attempt in $(seq 1 30); do
    if "${dc[@]}" exec -T redis redis-cli ping | grep -q PONG; then break; fi
    sleep 1
  done
  "${dc[@]}" exec -T redis redis-cli ping | grep -q PONG
  printf 'agent_monitor_pending='
  "${dc[@]}" exec -T redis redis-cli XLEN stage7:monitor:events
  printf 'agent_interpretations='
  "${dc[@]}" exec -T postgres psql -U powersystem -d powersystem -Atqc 'SELECT COUNT(*) FROM agent_interpretations'
  export V2M2_RESTORE_SERVICE_TOKEN="$restore_root/$run_id/config/agent/agent-service-token"
  "${dc[@]}" --profile agent-restore up -d --no-deps --wait emqx-restore
  "${dc[@]}" --profile agent-restore up -d --no-deps --wait restore-api
  for attempt in $(seq 1 30); do
    pending=$("${dc[@]}" exec -T redis redis-cli XLEN stage7:monitor:events)
    if [ "$pending" = 0 ]; then break; fi
    sleep 1
  done
  test "$pending" = 0
  printf 'restored_monitor_stream_drained=%s\n' "$pending"
  printf 'restored_monitor_events='
  "${dc[@]}" exec -T postgres psql -U powersystem -d powersystem -Atqc 'SELECT COUNT(*) FROM agent_monitor_events'
  "${dc[@]}" --profile agent-restore stop restore-api emqx-restore
fi
printf 'tdengine_rows='
"${dc[@]}" exec -T tdengine sh -c 'taos -p"$TAOS_ROOT_PASSWORD" -s "SELECT COUNT(*) FROM powersystem_stage2.telemetry"' | tail -n 3
snapshot_queues=$(find "$restore_root/$run_id/queues" -maxdepth 1 -name '*.sqlite' -type f | wc -l)
test "$snapshot_queues" -ge 2
printf 'snapshot_queues=%s\n' "$snapshot_queues"
printf 'restore_files=%s\n' "$restore_root/$run_id"
