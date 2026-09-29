#!/usr/bin/env bash
set -euo pipefail

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
env_file="$project_dir/.env"
compose_file="$project_dir/stages/04-frontend/deploy/compose.yaml"
backup_dir=${POWERSYSTEM_BACKUP_DIR:-/var/backups/powersystem}
passphrase_file=${POWERSYSTEM_BACKUP_PASSPHRASE_FILE:-/etc/powersystem/backup-passphrase}
backup_key=${POWERSYSTEM_BACKUP_SSH_KEY:-/etc/powersystem/backup-ed25519}
backup_target=${POWERSYSTEM_BACKUP_TARGET:-}
if [ -z "$backup_target" ] && [ -f /etc/powersystem/backup-target ]; then
  backup_target=$(cat /etc/powersystem/backup-target)
fi
if [ -z "$backup_target" ]; then
  echo 'Set POWERSYSTEM_BACKUP_TARGET or /etc/powersystem/backup-target before backup' >&2
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
chmod 600 "$staging/config/.env" "$staging/config/"*
(cd "$staging" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)

tar -C "$backup_dir" -czf "$backup_dir/$run_id.tar.gz" "$run_id"
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$passphrase_file" --symmetric --cipher-algo AES256 --output "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz"
(cd "$backup_dir" && sha256sum "$run_id.tar.gz.gpg" > "$run_id.tar.gz.gpg.sha256")
scp -o BatchMode=yes -o StrictHostKeyChecking=yes -i "$backup_key" "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz.gpg.sha256" "$backup_target"

find "$backup_dir" -maxdepth 1 -type f -name 'powersystem-*.tar.gz.gpg*' -mmin +10080 -delete
printf '%s\n' "$run_id"
