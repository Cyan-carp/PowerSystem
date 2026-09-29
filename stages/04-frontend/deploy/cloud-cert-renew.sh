#!/usr/bin/env bash
set -euo pipefail

cert_dir=/etc/letsencrypt
webroot=/usr/share/nginx/html
current=$(readlink -f "$cert_dir/live/8.138.10.222/cert.pem")
docker run --rm \
  -v "$cert_dir:/etc/letsencrypt" \
  -v "$webroot:/webroot" \
  certbot/certbot:v5.8.0 renew --non-interactive --quiet
updated=$(readlink -f "$cert_dir/live/8.138.10.222/cert.pem")
if [ "$current" != "$updated" ]; then
  nginx -t
  systemctl reload nginx
fi
openssl x509 -in "$cert_dir/live/8.138.10.222/fullchain.pem" -noout -checkend 172800
