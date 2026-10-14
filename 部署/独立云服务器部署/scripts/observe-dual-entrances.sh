#!/usr/bin/env bash
set -euo pipefail

samples=${1:-61}
interval=${2:-30}
output=${3:-/var/log/powersystem-edge-observe.csv}
[[ "$samples" =~ ^[0-9]+$ ]] && [ "$samples" -ge 2 ]
[[ "$interval" =~ ^[0-9]+$ ]] && [ "$interval" -ge 1 ]
umask 077
printf 'utc,old_http,new_http,local_http,old_frpc,new_frpc,api_restarts,agent_restarts\n' > "$output"
failures=0
baseline_api=$(docker inspect --format '{{.RestartCount}}' powersystem-stage4-api-1)
baseline_agent=$(docker inspect --format '{{.RestartCount}}' powersystem-stage4-agent-1)
for ((i=1; i<=samples; i++)); do
  now=$(date -u +%FT%TZ)
  old=$(curl --silent --show-error --max-time 6 --output /dev/null --write-out '%{http_code}' https://8.138.10.222/api/v1/ping 2>/dev/null || printf '000')
  new=$(curl --silent --show-error --max-time 6 --output /dev/null --write-out '%{http_code}' https://8.138.42.212/api/v1/ping 2>/dev/null || printf '000')
  local_code=$(curl --silent --show-error --max-time 6 --output /dev/null --write-out '%{http_code}' http://127.0.0.1:18080/api/v1/ping 2>/dev/null || printf '000')
  old_frpc=$(systemctl is-active frpc || true)
  new_frpc=$(systemctl is-active powersystem-edge-frpc || true)
  api_restarts=$(docker inspect --format '{{.RestartCount}}' powersystem-stage4-api-1)
  agent_restarts=$(docker inspect --format '{{.RestartCount}}' powersystem-stage4-agent-1)
  printf '%s,%s,%s,%s,%s,%s,%s,%s\n' "$now" "$old" "$new" "$local_code" "$old_frpc" "$new_frpc" "$api_restarts" "$agent_restarts" >> "$output"
  if [ "$old" != 200 ] || [ "$new" != 200 ] || [ "$local_code" != 200 ] ||
     [ "$old_frpc" != active ] || [ "$new_frpc" != active ] ||
     [ "$api_restarts" != "$baseline_api" ] || [ "$agent_restarts" != "$baseline_agent" ]; then
    failures=$((failures+1))
  fi
  if [ "$i" -lt "$samples" ]; then sleep "$interval"; fi
done
printf 'samples=%s failures=%s output=%s\n' "$samples" "$failures" "$output"
test "$failures" -eq 0
