#!/usr/bin/env bash
set -euo pipefail

config=/opt/frp/frpc.toml
frpc=/opt/frp/frpc
name=powersystem-stage4-web

"$frpc" verify -c "$config"
if grep -Fq "name = \"$name\"" "$config"; then
  echo "FRP proxy already exists; existing configuration left unchanged"
  exit 0
fi

backup="$config.pre-powersystem-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$config" "$backup"
cat >> "$config" <<'TOML'

[[proxies]]
name = "powersystem-stage4-web"
type = "tcp"
localIP = "127.0.0.1"
localPort = 18080
remotePort = 3667
TOML

if ! "$frpc" verify -c "$config"; then
  cp -p "$backup" "$config"
  echo "FRP validation failed; original config restored" >&2
  exit 1
fi
systemctl restart frpc
if ! systemctl is-active --quiet frpc; then
  cp -p "$backup" "$config"
  systemctl restart frpc
  echo "FRP failed to start; original config restored" >&2
  exit 1
fi
echo "FRP proxy $name added; original config saved at $backup"
