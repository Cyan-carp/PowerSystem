#!/bin/bash
set -eu
: "${TR_S01_FIXTURE_ROOT:?Set a dedicated directory ending in /tr-s01-fixture}"
case "$TR_S01_FIXTURE_ROOT" in /home/*/tr-s01-fixture) ;; *) echo 'dedicated fixture root required' >&2; exit 2;; esac
base="$TR_S01_FIXTURE_ROOT"
export LD_LIBRARY_PATH="$base/deps/root/usr/lib/x86_64-linux-gnu"
pgbin="$base/deps/root/usr/lib/postgresql/16/bin"
redisbin="$base/deps/root/usr/bin"
case "${1:-}" in
 start)
  mkdir -p "$base"
  test ! -e "$base/pg-current"
  "$pgbin/initdb" -D "$base/pg-current" -L "$base/deps/root/usr/share/postgresql/16" -U tr_s01 -A trust --no-locale > "$base/initdb.txt"
  "$pgbin/pg_ctl" -D "$base/pg-current" -l "$base/postgres.log" -o "-h 127.0.0.1 -p 15439 -k $base" start
  "$pgbin/createdb" -h 127.0.0.1 -p 15439 -U tr_s01 tr_s01_test
  "$redisbin/redis-server" --bind 127.0.0.1 --port 16389 --save '' --appendonly no --daemonize yes --pidfile "$base/redis.pid" --logfile "$base/redis.log"
  ;;
 reset)
  test -f "$base/pg-current/postmaster.pid"
  "$pgbin/dropdb" -h 127.0.0.1 -p 15439 -U tr_s01 --if-exists tr_s01_fresh_test
  "$pgbin/dropdb" -h 127.0.0.1 -p 15439 -U tr_s01 tr_s01_test
  "$pgbin/createdb" -h 127.0.0.1 -p 15439 -U tr_s01 tr_s01_test
  "$redisbin/redis-cli" -h 127.0.0.1 -p 16389 FLUSHDB
  ;;
 stop)
  test -f "$base/pg-current/postmaster.pid"
  "$pgbin/pg_ctl" -D "$base/pg-current" -m fast stop
  test -f "$base/redis.pid"
  "$redisbin/redis-cli" -h 127.0.0.1 -p 16389 shutdown nosave
  ;;
 *) echo 'start | reset | stop' >&2; exit 2;;
esac
