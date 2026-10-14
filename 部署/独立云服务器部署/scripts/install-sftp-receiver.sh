#!/usr/bin/env bash
set -euo pipefail

staged=${1:-/root/powersystem-edge-staging}
test -s "$staged/backup-ed25519.pub"
test -s "$staged/config/sshd-powersystem-backup.conf"
test -d /srv/powersystem-backups

if ! getent passwd powersystem-backup >/dev/null; then
  useradd --system --no-create-home --home-dir /incoming --shell /usr/sbin/nologin powersystem-backup
fi
install -d -o root -g root -m 755 /srv/powersystem-backups
install -d -o powersystem-backup -g powersystem-backup -m 700 /srv/powersystem-backups/incoming
install -d -o root -g root -m 755 /etc/ssh/authorized_keys
install -o root -g root -m 644 "$staged/backup-ed25519.pub" /etc/ssh/authorized_keys/powersystem-backup
install -o root -g root -m 644 "$staged/config/sshd-powersystem-backup.conf" /etc/ssh/sshd_config.d/powersystem-backup.conf
sshd -t
systemctl reload ssh
sshd -T -C user=powersystem-backup,host=localhost,addr=127.0.0.1 |
  grep -E '^(chrootdirectory|forcecommand|authorizedkeysfile|passwordauthentication|permittty|allowtcpforwarding) '
