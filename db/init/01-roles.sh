#!/bin/sh
# docker-entrypoint-initdb.d wrapper: bootstraps least-privilege roles and the
# `catan` / `catan_test` databases on first container init. Runs as the
# postgres superuser via the official postgres image's init mechanism.
set -eu

: "${MIGRATOR_PASSWORD:?MIGRATOR_PASSWORD must be set}"
: "${APP_PASSWORD:?APP_PASSWORD must be set}"

psql -v ON_ERROR_STOP=1 \
  -v migrator_password="$MIGRATOR_PASSWORD" \
  -v app_password="$APP_PASSWORD" \
  --username "$POSTGRES_USER" \
  -f /db/roles.sql
