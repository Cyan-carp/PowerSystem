#!/usr/bin/env bash
set -euo pipefail

host=8.138.42.212
expected='SHA256:PUS6mFBOfwdsITlg6Wxl0PP5FYg9wo/4pqrxADeJsyQ'
key=$(ssh-keyscan -T 8 -t ed25519 "$host" 2>/dev/null)
test -n "$key"
actual=$(printf '%s\n' "$key" | ssh-keygen -lf - | awk '{print $2}')
if [ "$actual" != "$expected" ]; then
  echo 'new cloud SSH host key does not match console fingerprint' >&2
  exit 1
fi
install -d -m 700 /root/.ssh
touch /root/.ssh/known_hosts
chmod 600 /root/.ssh/known_hosts
if ! grep -Fqx -- "$key" /root/.ssh/known_hosts; then
  printf '%s\n' "$key" >> /root/.ssh/known_hosts
fi
printf 'new_cloud_host_key=%s\n' "$actual"
