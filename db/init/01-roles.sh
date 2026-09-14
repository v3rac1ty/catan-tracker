#!/bin/sh
# docker-entrypoint-initdb.d wrapper: bootstraps least-privilege roles and the
# `catan` / `catan_test` databases on first container init. Runs as the
# postgres superuser via the official postgres image's init mechanism.
#
# Passwords are read by roles.sql itself via psql's \getenv, straight from
# the process environment -- never passed as -v arguments, so they never
# appear on the psql command line (e.g. in /proc/<pid>/cmdline).
set -eu

: "${MIGRATOR_PASSWORD:?MIGRATOR_PASSWORD must be set}"
: "${APP_PASSWORD:?APP_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  -f /db/roles.sql
