#!/usr/bin/env bash
set -euo pipefail

# Keep the FRP web port available only to the local TLS reverse proxy.
iptables -C INPUT -p tcp --dport 3667 ! -i lo -j DROP 2>/dev/null ||
  iptables -I INPUT -p tcp --dport 3667 ! -i lo -j DROP
ip6tables -C INPUT -p tcp --dport 3667 ! -i lo -j DROP 2>/dev/null ||
  ip6tables -I INPUT -p tcp --dport 3667 ! -i lo -j DROP
