#!/usr/bin/env bash
set -euo pipefail

# Missing allowlist files fail closed to loopback; values are private host config.
client_ip=$(cat /etc/powersystem-edge/allowed-client-ipv4 2>/dev/null || printf '127.0.0.1')
admin_ip=$(cat /etc/powersystem-edge/allowed-admin-ipv4 2>/dev/null || printf '127.0.0.1')
python3 -c 'import ipaddress,sys; [ipaddress.IPv4Address(v) for v in sys.argv[1:]]' "$client_ip" "$admin_ip"

iptables -N POWERSYSTEM_EDGE 2>/dev/null || true
iptables -F POWERSYSTEM_EDGE
iptables -A POWERSYSTEM_EDGE -i lo -j RETURN
iptables -A POWERSYSTEM_EDGE -p tcp --dport 3667 -j DROP
iptables -A POWERSYSTEM_EDGE -p tcp --dport 7000 -s "$client_ip/32" -j ACCEPT
iptables -A POWERSYSTEM_EDGE -p tcp --dport 7000 -j DROP
iptables -A POWERSYSTEM_EDGE -p tcp --dport 7070 -s "$admin_ip/32" -j ACCEPT
iptables -A POWERSYSTEM_EDGE -p tcp --dport 7070 -j DROP
iptables -C INPUT -j POWERSYSTEM_EDGE 2>/dev/null || iptables -I INPUT 1 -j POWERSYSTEM_EDGE

if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -N POWERSYSTEM_EDGE6 2>/dev/null || true
  ip6tables -F POWERSYSTEM_EDGE6
  ip6tables -A POWERSYSTEM_EDGE6 -i lo -j RETURN
  for port in 3667 7000 7070; do
    ip6tables -A POWERSYSTEM_EDGE6 -p tcp --dport "$port" -j DROP
  done
  ip6tables -C INPUT -j POWERSYSTEM_EDGE6 2>/dev/null || ip6tables -I INPUT 1 -j POWERSYSTEM_EDGE6
fi
