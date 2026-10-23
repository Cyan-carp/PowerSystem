#!/usr/bin/env bash
set -euo pipefail

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
# A backup and an application deployment must not snapshot different releases.
production_state="$project_dir/runtime/production-stack"
if [ -d "$production_state" ]; then
  exec 9>"$production_state/deployment.lock"
  flock -sn 9 || { echo 'Production deployment in progress; backup stopped' >&2; exit 2; }
fi
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
if [ -z "$backup_target" ] && [ "${POWERSYSTEM_BACKUP_LOCAL_ONLY:-0}" != "1" ]; then
  echo 'Set POWERSYSTEM_BACKUP_TARGET or /etc/powersystem/backup-target before backup' >&2
  exit 2
fi
if [ "${POWERSYSTEM_BACKUP_SKIP_SECONDARY:-0}" = "1" ]; then
  secondary_target=""
elif [ -z "$secondary_target" ] && [ -f /etc/powersystem/backup-secondary-target ]; then
  secondary_target=$(cat /etc/powersystem/backup-secondary-target)
fi
if [ -n "$secondary_target" ] && [ "$secondary_target" = "$backup_target" ]; then
  echo 'Primary and secondary backup targets must differ' >&2
  exit 2
fi
run_id="powersystem-$(date -u +%Y%m%dT%H%M%SZ)"
staging="$backup_dir/$run_id"

# Stage timing: one JSON line per stage, turned into $run_id.timing.json on exit.
# Only durations, byte counts and stage names are recorded, never command output.
timing_helper="$project_dir/stages/04-frontend/deploy/backup-timing.py"
stage_log="$backup_dir/$run_id.stages.ndjson"
timing_json="$backup_dir/$run_id.timing.json"
started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
script_start="$(date +%s%N)"
stage_name="preflight"
stage_start="$script_start"
failed_line=""
exit_status=0
staging_bytes=0
archive_bytes=0

bytes_of() {
  local total=0 path size
  for path in "$@"; do
    [ -e "$path" ] || continue
    size=$(du -sb "$path" | cut -f1)
    total=$(( total + size ))
  done
  printf '%s' "$total"
}

stage_begin() {
  stage_name="$1"
  stage_start="$(date +%s%N)"
}

stage_end() {
  local end
  end="$(date +%s%N)"
  printf '{"name":"%s","microseconds":%d,"offset_microseconds":%d,"bytes":%s}\n' \
    "$stage_name" "$(( (end - stage_start) / 1000 ))" \
    "$(( (stage_start - script_start) / 1000 ))" "${1:-0}" >> "$stage_log"
}

mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
: > "$stage_log"

cleanup() {
  rm -f -- "$backup_dir/$run_id.tar.gz"
  rm -rf -- "$staging"
}

finish() {
  local rc=$?
  trap - EXIT ERR
  set +e
  if [ "$rc" -eq 0 ] && [ "${exit_status:-0}" -ne 0 ]; then
    rc=1
  fi
  local status=ok
  local failed_stage=""
  if [ "$rc" -ne 0 ]; then
    status=failed
    failed_stage="${stage_name:-}"
  fi
  if [ -f "$timing_helper" ]; then
    python3 "$timing_helper" \
      --stages "$stage_log" --output "$timing_json" --run-id "$run_id" \
      --status "$status" --failed-stage "$failed_stage" --failed-line "${failed_line:-}" \
      --archive "$backup_dir/$run_id.tar.gz.gpg" --staging-bytes "${staging_bytes:-0}" \
      --started-at "$started_at" --host "$(hostname)" >/dev/null \
      || echo 'Backup timing record could not be written' >&2
  else
    echo "Backup timing helper missing at $timing_helper; no timing record" >&2
  fi
  rm -f -- "$stage_log"
  cleanup
  exit "$rc"
}
trap 'failed_line=$LINENO; exit_status=1' ERR
trap finish EXIT

stage_begin preflight
test -f "$env_file"
test -r "$passphrase_file"
test -r "$backup_key"
for private_file in /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret /etc/powersystem/business-notify-token; do
  test -s "$private_file"
done
mkdir -m 700 "$staging"
mkdir -m 700 "$staging/tdengine"
mkdir -m 700 "$staging/config"
stage_end 0

# A full staging tree plus archive needs roughly three times the archive size;
# stop before dumping instead of failing on a full disk after ten minutes.
stage_begin disk_headroom
available_kb=$(df -Pk "$backup_dir" | awk 'NR==2 {print $4}')
minimum_kb=${POWERSYSTEM_BACKUP_MIN_FREE_KB:-4194304}
if [ -z "$available_kb" ] || [ "$available_kb" -lt "$minimum_kb" ]; then
  echo "Free space under $backup_dir is ${available_kb:-unknown}KB, ${minimum_kb}KB required" >&2
  exit 2
fi
stage_end "$(( available_kb * 1024 ))"
dc=(docker compose --env-file "$env_file" -f "$compose_file")

stage_begin postgres_dump
"${dc[@]}" exec -T postgres pg_dump -U powersystem -d powersystem -Fc > "$staging/postgres.dump"
stage_end "$(bytes_of "$staging/postgres.dump")"

stage_begin tdengine_dump
"${dc[@]}" exec -T tdengine sh -c 'taosdump -D powersystem_stage2 -p"$TAOS_ROOT_PASSWORD" -o "$1"' sh "/backup/$run_id/tdengine"
stage_end "$(bytes_of "$staging/tdengine")"

stage_begin queues_snapshot
"${dc[@]}" --profile maintenance run --rm --no-deps backup-helper python /scripts/snapshot_queues.py "/backup/$run_id/queues"
stage_end "$(bytes_of "$staging/queues")"

stage_begin model_copy
cp "$project_dir/runtime/model/model.json" "$project_dir/runtime/model/metadata.json" "$staging/"
stage_end "$(bytes_of "$staging/model.json" "$staging/metadata.json")"

stage_begin config_copy
cp "$env_file" "$staging/config/.env"
cp /etc/powersystem/feishu-webhook-url /etc/powersystem/feishu-sign-secret /etc/powersystem/business-notify-token "$staging/config/"
stage_end "$(bytes_of "$staging/config")"

if [ -s /etc/powersystem/agent-service-token ]; then
  stage_begin agent_credentials
  mkdir -m 700 "$staging/config/agent"
  if [ -s /etc/powersystem/interpret-deepseek-key ]; then
    cp -p /etc/powersystem/interpret-deepseek-key "$staging/config/agent/"
  fi
  for agent_file in agent-service-token agent-monitor-token agent-model-key; do
    test -f "/etc/powersystem/$agent_file"
    cp "/etc/powersystem/$agent_file" "$staging/config/agent/"
  done
  stage_end "$(bytes_of "$staging/config/agent")"

  stage_begin redis_snapshot
  "${dc[@]}" exec -T redis redis-cli --rdb /tmp/powersystem-agent-backup.rdb >/dev/null
  redis_container=$("${dc[@]}" ps -q redis)
  docker cp "$redis_container:/tmp/powersystem-agent-backup.rdb" "$staging/redis.rdb"
  "${dc[@]}" exec -T redis rm -f /tmp/powersystem-agent-backup.rdb
  stage_end "$(bytes_of "$staging/redis.rdb")"

  stage_begin agent_audit
  if [ -d "$project_dir/runtime/agent-audit" ]; then
    cp -a "$project_dir/runtime/agent-audit" "$staging/agent-audit"
  fi
  stage_end "$(bytes_of "$staging/agent-audit")"

  stage_begin agent_knowledge
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
  stage_end "$(bytes_of "$staging/agent-knowledge-misses" "$staging/knowledge-index.json")"

  stage_begin manuals
  # Manufacturer originals are private runtime assets, never release files.
  manuals_dir=${POWERSYSTEM_MANUALS_DIR:-$project_dir/runtime/manuals}
  if [ -d "$manuals_dir" ]; then
    manuals_manifest="$project_dir/notes/05-设备知识设计素材与历史归档资产/设备知识/手册来源清单.json"
    test -s "$manuals_manifest"
    cp -a "$manuals_dir" "$staging/manuals"
    cp "$manuals_manifest" "$staging/manuals-manifest.json"
  fi
  stage_end "$(bytes_of "$staging/manuals" "$staging/manuals-manifest.json")"
fi
if docker inspect powersystem-stage4-agent-rag-1 >/dev/null 2>&1; then
  stage_begin rag_vectors
  docker exec -i powersystem-stage4-agent-rag-1 python - < "$project_dir/stages/08-agent-rag/deploy/backup-rag.py" > "$staging/rag-vectors.tar"
  stage_end "$(bytes_of "$staging/rag-vectors.tar")"

  stage_begin rag_postgres
  "${dc[@]}" exec -T postgres pg_dump -U powersystem -d powersystem_agent_rag -Fc > "$staging/rag-postgres.dump"
  stage_end "$(bytes_of "$staging/rag-postgres.dump")"

  stage_begin rag_model
  rag_model_dir=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/models/multilingual-e5-small"}}{{.Source}}{{end}}{{end}}' powersystem-stage4-agent-rag-1)
  cp -a "$rag_model_dir" "$staging/rag-model"
  stage_end "$(bytes_of "$staging/rag-model")"

  stage_begin rag_environment
  rag_env_file=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.environment_file"}}' powersystem-stage4-agent-rag-1)
  # Incremental releases keep compose.env in a new directory while RAG_ENV_FILE
  # still points to the original runtime environment. Follow the active setting.
  rag_runtime_env_file=$(python3 - "$rag_env_file" "$project_dir" <<'PY'
import pathlib,sys
compose=pathlib.Path(sys.argv[1]);root=pathlib.Path(sys.argv[2])
values=dict(line.split('=',1) for line in compose.read_text().splitlines() if '=' in line and not line.lstrip().startswith('#'))
value=values['RAG_ENV_FILE'].strip().strip("\"'")
path=pathlib.Path(value);path=path if path.is_absolute() else root/path
assert path.is_file() and path.resolve().is_relative_to(root/'runtime')
print(path)
PY
  )
  cp "$rag_runtime_env_file" "$staging/config/agent/rag.env"
  cp "$rag_env_file" "$staging/config/agent/rag-compose.env"
  cp -a "$project_dir/runtime/agent-rag-audit" "$staging/rag-audit"
  stage_end "$(bytes_of "$staging/config/agent/rag.env" "$staging/config/agent/rag-compose.env" "$staging/rag-audit")"
fi
# M4 is optional for old releases; detected active service requires complete backup.
if docker inspect powersystem-stage4-ops-agent-1 >/dev/null 2>&1; then
  stage_begin ops_assets
  python3 "$project_dir/stages/09-ops-agent/deploy/backup-ma.py" --output "$staging"
  stage_end "$(bytes_of "$staging/ma")"
fi
if [ -d "$production_state" ]; then
  stage_begin production_state
  # These files can contain resolved secrets. They enter the AES256 archive only.
  cp -a "$production_state" "$staging/production-stack"
  cp "$project_dir/stages/04-frontend/deploy/production-stack.py" "$staging/production-stack/controller.py"
  stage_end "$(bytes_of "$staging/production-stack")"
fi

stage_begin staging_permissions
find "$staging/config" -type f -exec chmod 600 {} +
find "$staging/config" -type d -exec chmod 700 {} +
stage_end 0

stage_begin staging_manifest
staging_bytes=$(bytes_of "$staging")
(cd "$staging" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
stage_end "$staging_bytes"

stage_begin compress
tar -C "$backup_dir" -czf "$backup_dir/$run_id.tar.gz" "$run_id"
stage_end "$(bytes_of "$backup_dir/$run_id.tar.gz")"

stage_begin encrypt
gpg --batch --yes --pinentry-mode loopback --passphrase-file "$passphrase_file" --symmetric --cipher-algo AES256 --output "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz"
stage_end "$(bytes_of "$backup_dir/$run_id.tar.gz.gpg")"

stage_begin archive_digest
archive_bytes=$(bytes_of "$backup_dir/$run_id.tar.gz.gpg")
(cd "$backup_dir" && sha256sum "$run_id.tar.gz.gpg" > "$run_id.tar.gz.gpg.sha256")
stage_end "$archive_bytes"

upload_failed=0
if [ "${POWERSYSTEM_BACKUP_LOCAL_ONLY:-0}" = "1" ]; then
  echo 'Local-only encrypted backup retained; no upload performed'
else
  stage_begin upload_primary
  if scp -o BatchMode=yes -o StrictHostKeyChecking=yes -i "$backup_key" "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz.gpg.sha256" "$backup_target"; then
    echo 'Primary backup upload succeeded'
  else
    echo 'Primary backup upload failed; local encrypted archive retained' >&2
    upload_failed=1
  fi
  stage_end "$archive_bytes"
fi
if [ "${POWERSYSTEM_BACKUP_LOCAL_ONLY:-0}" != "1" ] && [ -n "$secondary_target" ]; then
  stage_begin upload_secondary
  if scp -o BatchMode=yes -o StrictHostKeyChecking=yes -i "$backup_key" "$backup_dir/$run_id.tar.gz.gpg" "$backup_dir/$run_id.tar.gz.gpg.sha256" "$secondary_target"; then
    echo 'Secondary backup upload succeeded'
  else
    echo 'Secondary backup upload failed; local encrypted archive retained' >&2
    upload_failed=1
  fi
  stage_end "$archive_bytes"
fi
if [ "$upload_failed" -ne 0 ]; then
  exit 1
fi

stage_begin retention
find "$backup_dir" -maxdepth 1 -type f -name 'powersystem-*.tar.gz.gpg*' -mmin +10080 -delete
find "$backup_dir" -maxdepth 1 -type f -name 'powersystem-*.timing.json' -mmin +10080 -delete
stage_end 0

printf '%s\n' "$run_id"
