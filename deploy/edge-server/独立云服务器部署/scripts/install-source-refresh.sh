#!/usr/bin/env bash
# Prepare only: never enables services, changes iptables, or reloads Nginx.
set -euo pipefail
umask 077
[[ $(id -u) == 0 ]] || { echo 'root_required' >&2; exit 1; }
role=${1:?usage: install-source-refresh.sh cloud|client}
[[ "$role" == cloud || "$role" == client ]] || exit 2
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
config_dir="$script_dir/../config"
target=/opt/powersystem-edge/source-refresh
if [[ "$role" == cloud ]]; then
  pin=$(ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub | awk '{print $2}')
  [[ "$pin" == SHA256:PUS6mFBOfwdsITlg6Wxl0PP5FYg9wo/4pqrxADeJsyQ ]] || { echo 'wrong_cloud' >&2; exit 1; }
  for unit in powersystem-source-web.service powersystem-source-executor.service; do
    if systemctl is-active --quiet "$unit"; then echo 'active_install_requires_maintenance' >&2; exit 1; fi
  done
  id powersystem-source >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin powersystem-source
  install -d -o root -g root -m 700 /var/lib/powersystem-source
else
  [[ $(hostname) == R730XD ]] || { echo 'wrong_client_host' >&2; exit 1; }
  if systemctl is-active --quiet powersystem-source-refresh.timer; then echo 'active_install_requires_maintenance' >&2; exit 1; fi
  install -d -o root -g root -m 700 /var/lib/powersystem-source-client
fi
install -d -o root -g root -m 755 "$target" "$target/frp_source" "$target/templates"
install -o root -g root -m 755 "$script_dir/frp-source-refresh.py" "$target/frp-source-refresh.py"
for file in "$script_dir"/frp_source/*.py; do install -o root -g root -m 644 "$file" "$target/frp_source/"; done
install -o root -g root -m 644 "$script_dir/README.md" "$target/README.md"
for file in "$config_dir"/source-refresh-* "$config_dir"/powersystem-source-*; do
  install -o root -g root -m 644 "$file" "$target/templates/"
done
if [[ "$role" == cloud ]]; then
  for unit in powersystem-source-executor.service powersystem-source-web.service; do
    install -o root -g root -m 644 "$config_dir/$unit" "/etc/systemd/system/$unit"
  done
  if [[ ! -e /etc/powersystem-edge/source-refresh-cloud.json ]]; then
    install -o root -g root -m 600 "$config_dir/source-refresh-cloud.example.json" /etc/powersystem-edge/source-refresh-cloud.json
  fi
else
  for unit in powersystem-source-refresh.service powersystem-source-refresh.timer; do
    install -o root -g root -m 644 "$config_dir/$unit" "/etc/systemd/system/$unit"
  done
  if [[ ! -e /etc/powersystem-edge/source-refresh-client.json ]]; then
    install -o root -g root -m 600 "$config_dir/source-refresh-client.example.json" /etc/powersystem-edge/source-refresh-client.json
  fi
fi
systemctl daemon-reload
echo "prepared_role=$role activated=false"
