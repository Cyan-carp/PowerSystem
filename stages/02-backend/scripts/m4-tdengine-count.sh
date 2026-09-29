#!/bin/sh
set -eu
exec taos -u root -p"$TAOS_ROOT_PASSWORD" -s 'SELECT count(*) FROM powersystem_stage2.telemetry'
