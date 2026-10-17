#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ] || ! [[ "$1" =~ ^[0-9]+$ ]]; then
  echo 'usage: check-backup-space.sh LATEST_ENCRYPTED_BYTES RECEIVER_DIRECTORY' >&2
  exit 2
fi
latest_bytes=$1
receiver_dir=$2
test -d "$receiver_dir"
test "$latest_bytes" -gt 0
if [ "$latest_bytes" -gt 307445734561825860 ]; then
  echo 'backup size exceeds safe integer range' >&2
  exit 2
fi
required=$((latest_bytes * 30))
if [ "$required" -lt 10737418240 ]; then required=10737418240; fi
available=$(df -B1 --output=avail "$receiver_dir" | tail -n 1 | tr -d '[:space:]')
[[ "$available" =~ ^[0-9]+$ ]]
printf 'latest_bytes=%s\nrequired_bytes=%s\navailable_bytes=%s\n' "$latest_bytes" "$required" "$available"
if [ "$available" -lt "$required" ]; then
  echo 'FAIL: new cloud backup receiver has insufficient free space' >&2
  exit 1
fi
echo 'PASS: new cloud backup receiver has sufficient free space'
