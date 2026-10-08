#!/usr/bin/env bash
set -euo pipefail

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
env_file="$project_dir/.env"
compose_file="$project_dir/stages/04-frontend/deploy/compose.yaml"
backup_dir=${POWERSYSTEM_BACKUP_DIR:-/var/backups/powersystem}
passphrase_file=${POWERSYSTEM_BACKUP_PASSPHRASE_FILE:-/etc/powersystem/backup-passphrase}
backup_key=${POWERSYSTEM_BACKUP_SSH_KEY:-/etc/powersystem/backup-ed25519}
backup_target=${POWERSYSTEM_BACKUP_TARGET:-}
secondary_target=${POWERSYSTEM_BACKUP_SECONDARY_TARGET:-}
if [ -z "$backup_target" ] && [ -f /etc/powersystem/backup-target ]; then
  backup_target=$(cat /etc/powersystem/backup-target)
fi
if [ -z "$backup_target" ]; then
  echo 'Set POWERSYSTEM_BACKUP_TARGET or /etc/powersystem/backup-target before backup' >&2
  exit 2
fi
if [ -z "$secondary_target" ] && [ -f /etc/powersystem/backup-secondary-target ]; then
  secondary_target=$(cat /etc/powersystem/backup-secondary-target)
fi
if [ -n "$secondary_target" ] && [ "$secondary_target" = "$backup_target" ]; then
  echo 'Primary and secondary backup targets must differ' >&2
  exit 2
fi
run_id="powersystem-$(date -u +%Y%m%dT%H%M%SZ)"
staging="$backup_dir/$run_id"

test -f "$env_file"
test -r "$passphrase_file"
test -r "$backup_key"
for private_file in /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret /etc/powersystem/business-notify-token; do
  test -s "$private_file"
done
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
mkdir -m 700 "$staging"
mkdir -m 700 "$staging/tdengine"
mkdir -m 700 "$staging/config"
cleanup() {
  rm -f -- "$backup_dir/$run_id.tar.gz"
  rm -rf -- "$staging"
}
trap cleanup EXIT
dc=(docker compose --env-file "$env_file" -f "$compose_file")

"${dc[@]}" exec -T postgres pg_dump -U powersystem -d powersystem -Fc > "$staging/postgres.dump"
"${dc[@]}" exec -T tdengine sh -c 'taosdump -D powersystem_stage2 -p"$TAOS_ROOT_PASSWORD" -o "$1"' sh "/backup/$run_id/tdengine"
"${dc[@]}" --profile maintenance run --rm --no-deps backup-helper python /scripts/snapshot_queues.py "/backup/$run_id/queues"
cp "$project_dir/runtime/model/model.json" "$project_dir/runtime/model/metadata.json" "$staging/"
cp "$env_file" "$staging/config/.env"
cp /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret /etc/powersystem/business-notify-token "$staging/config/"
if [ -s /etc/powersystem/agent-service-token ]; then
  mkdir -m 700 "$staging/config/agent"
  for agent_file in agent-service-token agent-monitor-token agent-model-key; do
    test -f "/etc/powersystem/$agent_file"
    cp "/etc/powersystem/$agent_file" "$staging/config/agent/"
  done
  "${dc[@]}" exec -T redis redis-cli --rdb /tmp/powersystem-agent-backup.rdb >/dev/null
  redis_container=$("${dc[@]}" ps -q redis)
  docker cp "$redis_container:/tmp/powersystem-agent-backup.rdb" "$staging/redis.rdb"
  "${dc[@]}" exec -T redis rm -f /tmp/powersystem-agent-backup.rdb
  if [ -d "$project_dir/runtime/agent-audit" ]; then
    cp -a "$project_dir/runtime/agent-audit" "$staging/agent-audit"
  fi
  if [ -f /etc/powersystem/agent-search-key ]; then
    cp /etc/powersystem/agent-search-key "$staging/config/agent/"
  fi
  # Use the actual bind source, including private AGENT_MISSES_HOST_DIR overrides.
  agent_misses_dir="$project_dir/runtime/agent-knowledge-misses"
  if docker inspect powersystem-stage4-agent-1 >/dev/null 2>&1; then
    mounted_misses=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/knowledge-misses"}}{{.Source}}{{end}}{{end}}' powersystem-stage4-agent-1)
    if [ -n "$mounted_misses" ]; then agent_misses_dir="$mounted_misses"; fi
  fi
  if [ -d "$agent_misses_dir" ]; then
    cp -a "$agent_misses_dir" "$staging/agent-knowledge-misses"
  fi
  if [ -f "$project_dir/stages/07-agent/knowledge/index.json" ]; then
    cp "$project_dir/stages/07-agent/knowledge/index.json" "$staging/knowledge-index.json"
  fi
fi
find "$staging/config" -type f -exec chmod 600 {} +
find "$staging/config" -type d -exec chmod 700 {} +
(cd "$staging" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)

tar -C "$backup_dir" -czf "$backup_dir/$run_id.tar.gz" "$run_id"
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$passphrase_file" --symmetric --cipher-algo AES256 --output "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz"
(cd "$backup_dir" && sha256sum "$run_id.tar.gz.gpg" > "$run_id.tar.gz.gpg.sha256")
upload_failed=0
if scp -o BatchMode=yes -o StrictHostKeyChecking=yes -i "$backup_key" "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz.gpg.sha256" "$backup_target"; then
  echo 'Primary backup upload succeeded'
else
  echo 'Primary backup upload failed; local encrypted archive retained' >&2
  upload_failed=1
fi
if [ -n "$secondary_target" ]; then
  if scp -o BatchMode=yes -o StrictHostKeyChecking=yes -i "$backup_key" "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz.gpg.sha256" "$secondary_target"; then
    echo 'Secondary backup upload succeeded'
  else
    echo 'Secondary backup upload failed; local encrypted archive retained' >&2
    upload_failed=1
  fi
fi
if [ "$upload_failed" -ne 0 ]; then
  exit 1
fi

find "$backup_dir" -maxdepth 1 -type f -name 'powersystem-*.tar.gz.gpg*' -mmin +10080 -delete
printf '%s\n' "$run_id"
