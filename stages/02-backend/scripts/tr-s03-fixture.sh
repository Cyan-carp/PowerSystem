#!/bin/bash
set -eu
: "${TR_S03_FIXTURE_ROOT:?Set dedicated /home/.../tr-s03-fixture}"
: "${TR_S03_DEPS_ROOT:?Set extracted dependency root}"
case "$TR_S03_FIXTURE_ROOT" in /home/*/tr-s03-fixture) ;; *) exit 2;; esac
case "$TR_S03_DEPS_ROOT" in /home/*/tr-s01-fixture/deps/root) ;; *) exit 2;; esac
base="$TR_S03_FIXTURE_ROOT"
export LD_LIBRARY_PATH="$TR_S03_DEPS_ROOT/usr/lib/x86_64-linux-gnu"
pgbin="$TR_S03_DEPS_ROOT/usr/lib/postgresql/16/bin"
redisbin="$TR_S03_DEPS_ROOT/usr/bin"
case "${1:-}" in
 start)
  mkdir -p "$base"
  test ! -e "$base/pg/postmaster.pid"
  if [ ! -f "$base/pg/PG_VERSION" ]; then
   "$pgbin/initdb" -D "$base/pg" -L "$TR_S03_DEPS_ROOT/usr/share/postgresql/16" -U tr_s03 -A trust --no-locale > "$base/initdb.txt"
  fi
  "$pgbin/pg_ctl" -D "$base/pg" -l "$base/postgres.log" -o "-h 127.0.0.1 -p 15441 -k $base" start
  if ! "$pgbin/psql" -h 127.0.0.1 -p 15441 -U tr_s03 postgres -Atc "SELECT datname FROM pg_database" | grep -qx tr_s03_test; then
   "$pgbin/createdb" -h 127.0.0.1 -p 15441 -U tr_s03 tr_s03_test
  fi
  "$redisbin/redis-server" --bind 127.0.0.1 --port 16391 --save '' --appendonly no --daemonize yes --pidfile "$base/redis.pid" --logfile "$base/redis.log"
  ;;
 stop)
  test -f "$base/pg/postmaster.pid"
  "$pgbin/pg_ctl" -D "$base/pg" -m fast stop
  test -f "$base/redis.pid"
  actual=$("$redisbin/redis-cli" -h 127.0.0.1 -p 16391 --raw INFO server | tr -d '\r' | awk -F: '$1=="process_id" {print $2}')
  test "$actual" = "$(cat "$base/redis.pid")"
  "$redisbin/redis-cli" -h 127.0.0.1 -p 16391 shutdown nosave
  ;;
 *) echo 'start | stop' >&2; exit 2;;
esac
