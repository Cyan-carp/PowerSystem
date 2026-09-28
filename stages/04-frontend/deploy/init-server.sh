#!/usr/bin/env bash
set -euo pipefail

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
install -d -m 700 "$project_dir/runtime/model" /var/backups/powersystem /etc/powersystem
env_file="$project_dir/.env"
if [ ! -e "$env_file" ]; then
  umask 077
  {
    printf 'TDENGINE_ROOT_PASSWORD=%s\n' "Aa1_$(openssl rand -hex 20)"
    printf 'POSTGRES_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'REDIS_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'JWT_SECRET=%s\n' "$(openssl rand -hex 32)"
    printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)"
    printf 'POWERSYSTEM_MODEL_DIR=%s\n' "$project_dir/runtime/model"
    printf 'POWERSYSTEM_BACKUP_DIR=/var/backups/powersystem\n'
  } > "$env_file"
  chmod 600 "$env_file"
fi
if [ ! -e /etc/powersystem/backup-passphrase ]; then
  umask 077
  openssl rand -hex 48 > /etc/powersystem/backup-passphrase
  chmod 600 /etc/powersystem/backup-passphrase
fi
echo "PowerSystem directories and private configuration are ready"
