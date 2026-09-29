#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^[A-Za-z0-9_-]{3,64}$ ]]; then
  echo 'Usage: bootstrap-admin.sh <new-admin-username>' >&2
  exit 2
fi
read -r -s -p 'New admin password (12-72 characters): ' password
printf '\n'
read -r -s -p 'Confirm password: ' confirm
printf '\n'
if [ "$password" != "$confirm" ] || [ "${#password}" -lt 12 ] || [ "${#password}" -gt 72 ]; then
  echo 'Password confirmation or length invalid' >&2
  exit 2
fi
printf '%s\n' "$password" | docker compose --env-file .env -f stages/04-frontend/deploy/compose.yaml exec -T api /app/server "-create-admin=$1"
unset password confirm
