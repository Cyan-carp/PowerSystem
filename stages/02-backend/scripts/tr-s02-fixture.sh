#!/bin/bash
set -eu
: "${TR_S02_FIXTURE_ROOT:?Set dedicated /home/.../tr-s02-fixture}"
: "${TR_S02_DEPS_ROOT:?Set extracted dependency root}"
case "$TR_S02_FIXTURE_ROOT" in /home/*/tr-s02-fixture) ;; *) exit 2;; esac
case "$TR_S02_DEPS_ROOT" in /home/*/tr-s01-fixture/deps/root) ;; *) exit 2;; esac
base="$TR_S02_FIXTURE_ROOT"
export LD_LIBRARY_PATH="$TR_S02_DEPS_ROOT/usr/lib/x86_64-linux-gnu"
pgbin="$TR_S02_DEPS_ROOT/usr/lib/postgresql/16/bin"
redisbin="$TR_S02_DEPS_ROOT/usr/bin"
case "${1:-}" in
 start)
  mkdir -p "$base"
  test ! -e "$base/pg/postmaster.pid"
  if [ ! -f "$base/pg/PG_VERSION" ]; then
   "$pgbin/initdb" -D "$base/pg" -L "$TR_S02_DEPS_ROOT/usr/share/postgresql/16" -U tr_s02 -A trust --no-locale > "$base/initdb.txt"
  fi
  "$pgbin/pg_ctl" -D "$base/pg" -l "$base/postgres.log" -o "-h 127.0.0.1 -p 15440 -k $base" start
  if ! "$pgbin/psql" -h 127.0.0.1 -p 15440 -U tr_s02 postgres -Atc "SELECT datname FROM pg_database" | grep -qx tr_s02_test; then
   "$pgbin/createdb" -h 127.0.0.1 -p 15440 -U tr_s02 tr_s02_test
  fi
  "$redisbin/redis-server" --bind 127.0.0.1 --port 16390 --save '' --appendonly no --daemonize yes --pidfile "$base/redis.pid" --logfile "$base/redis.log"
  ;;
 stop)
  test -f "$base/pg/postmaster.pid"
  "$pgbin/pg_ctl" -D "$base/pg" -m fast stop
  test -f "$base/redis.pid"
  actual=$("$redisbin/redis-cli" -h 127.0.0.1 -p 16390 --raw INFO server | tr -d '\r' | awk -F: '$1=="process_id" {print $2}')
  test "$actual" = "$(cat "$base/redis.pid")"
  "$redisbin/redis-cli" -h 127.0.0.1 -p 16390 shutdown nosave
  ;;
 *) echo 'start | stop' >&2; exit 2;;
esac
