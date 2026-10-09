#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo 'usage: install-reference-manual.sh /private/uploaded-manual.pdf' >&2
  exit 2
fi

project_dir=${POWERSYSTEM_DIR:-/opt/powersystem}
manuals_dir=${POWERSYSTEM_MANUALS_DIR:-$project_dir/runtime/manuals}
manifest="$project_dir/设备知识/手册来源清单.json"
source_file=$1
case "$manuals_dir" in /*) ;; *) echo 'manuals directory must be absolute' >&2; exit 2 ;; esac
test -f "$source_file"
test -s "$manifest"

read -r expected_name expected_size expected_sha < <(python3 - "$manifest" <<'PY'
import json, pathlib, sys
item = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['manuals'][0]
print(pathlib.Path(item['private_storage']).name, item['file_size_bytes'], item['sha256'])
PY
)
test "$(stat -c %s -- "$source_file")" = "$expected_size"
test "$(sha256sum -- "$source_file" | cut -d ' ' -f1)" = "$expected_sha"

install -d -m 700 -- "$manuals_dir"
install -m 600 -- "$source_file" "$manuals_dir/$expected_name"
test "$(sha256sum -- "$manuals_dir/$expected_name" | cut -d ' ' -f1)" = "$expected_sha"
printf 'manual_installed=%s sha256=%s\n' "$manuals_dir/$expected_name" "$expected_sha"
