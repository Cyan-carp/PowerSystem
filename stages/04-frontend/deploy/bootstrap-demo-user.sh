#!/usr/bin/env bash
set -euo pipefail

password_file=/etc/powersystem/demo-password
if [ -e "$password_file" ]; then
  echo 'Demo credentials already exist; no account changed'
  exit 0
fi

password=$(openssl rand -hex 18)
body=$(printf '{"username":"demo-operator","password":"%s","real_name":"Demo Operator"}' "$password")
response=$(printf '%s' "$body" | docker exec -i powersystem-stage4-api-1 wget -qO- --header 'Content-Type: application/json' --post-file=- http://127.0.0.1:8080/api/v1/auth/register)
case "$response" in
  *'"code":0'*) ;;
  *) echo 'Demo registration did not return success' >&2; exit 1 ;;
esac
umask 077
printf '%s\n' "$password" > "$password_file"
chmod 600 "$password_file"
echo 'Demo account created; username demo-operator, password stored privately on R730xd'
