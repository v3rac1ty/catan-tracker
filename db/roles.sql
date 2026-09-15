-- Least-privilege role & database bootstrap for Catan Tracker.
--
-- Run once by the `postgres` superuser (via docker-entrypoint-initdb.d, see
-- db/init/01-roles.sh, or manually with `psql`). Idempotent: safe to re-run.
--
-- Passwords are read from the MIGRATOR_PASSWORD / APP_PASSWORD environment
-- variables with \getenv, so they never appear on the psql command line (and
-- therefore never show up in /proc/<pid>/cmdline or shell history). The
-- script fails hard (ON_ERROR_STOP) if either is missing.
--
-- Two roles:
--   catan_migrator  owns the schema, runs migrations (DDL).
--   catan_app       runtime role used by the bot: SELECT/INSERT/UPDATE only,
--                   no DELETE/TRUNCATE/DROP/ALTER/CREATE.
--
-- Two databases: `catan` (production) and `catan_test` (integration tests).

\set ON_ERROR_STOP on

\getenv migrator_password MIGRATOR_PASSWORD
\getenv app_password APP_PASSWORD

\if :{?migrator_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'MIGRATOR_PASSWORD not set'; END $$;
\endif

\if :{?app_password}
\else
DO $$ BEGIN RAISE EXCEPTION 'APP_PASSWORD not set'; END $$;
\endif

-- `\getenv` only tells us the variable was present; an empty string (e.g.
-- MIGRATOR_PASSWORD="") still satisfies :{?var} above but must not silently
-- create a password-less role.
SELECT :'migrator_password' = '' AS migrator_password_empty
\gset

\if :migrator_password_empty
DO $$ BEGIN RAISE EXCEPTION 'MIGRATOR_PASSWORD is empty'; END $$;
\endif

SELECT :'app_password' = '' AS app_password_empty
\gset

\if :app_password_empty
DO $$ BEGIN RAISE EXCEPTION 'APP_PASSWORD is empty'; END $$;
\endif

-- A short password is still a weak password even though it's non-empty.
-- 16 chars is a conservative floor; `openssl rand -hex 24` (recommended in
-- .env.example) produces 48.
SELECT length(:'migrator_password') < 16 AS migrator_password_too_short
\gset

\if :migrator_password_too_short
DO $$ BEGIN RAISE EXCEPTION 'MIGRATOR_PASSWORD is too short (must be at least 16 characters)'; END $$;
\endif

SELECT length(:'app_password') < 16 AS app_password_too_short
\gset

\if :app_password_too_short
DO $$ BEGIN RAISE EXCEPTION 'APP_PASSWORD is too short (must be at least 16 characters)'; END $$;
\endif

-- ---------------------------------------------------------------------------
-- Roles (idempotent create-if-not-exists)
-- ---------------------------------------------------------------------------

SELECT format(
    'CREATE ROLE catan_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L',
    :'migrator_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catan_migrator')
\gexec

SELECT format(
    'CREATE ROLE catan_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'catan_app')
\gexec

ALTER ROLE catan_app SET statement_timeout = '5s';

-- ---------------------------------------------------------------------------
-- Databases, owned by the migrator role (idempotent create-if-not-exists).
--
-- ENCODING 'UTF8' TEMPLATE template0 is pinned explicitly (rather than
-- inheriting whatever template1 happens to have) so Python `len()` always
-- matches Postgres `char_length()` on stored text -- required for the
-- CHECK (char_length(...) BETWEEN ...) constraints in 0001_init.sql to mean
-- what the application code assumes. LC_COLLATE/LC_CTYPE are left at the
-- cluster's initdb defaults (UTF8 is compatible with any locale), so this
-- only pins encoding, not collation/ctype.
-- ---------------------------------------------------------------------------

SELECT format('CREATE DATABASE catan OWNER catan_migrator ENCODING %L TEMPLATE template0', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'catan')
\gexec

SELECT format(
    'CREATE DATABASE catan_test OWNER catan_migrator ENCODING %L TEMPLATE template0', 'UTF8'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'catan_test')
\gexec

-- ---------------------------------------------------------------------------
-- Nobody should use the cluster's default maintenance databases.
-- ---------------------------------------------------------------------------

REVOKE CONNECT, TEMP ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT, TEMP ON DATABASE template1 FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- Per-database privileges. catan_app gets CONNECT + USAGE on schema public,
-- and (via default privileges owned by catan_migrator) SELECT/INSERT/UPDATE
-- on future tables and USAGE/SELECT on future sequences. No DDL, no DELETE.
-- ---------------------------------------------------------------------------

\connect catan

REVOKE ALL ON DATABASE catan FROM PUBLIC;
GRANT CONNECT ON DATABASE catan TO catan_migrator, catan_app;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO catan_app;

ALTER DEFAULT PRIVILEGES FOR ROLE catan_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO catan_app;
ALTER DEFAULT PRIVILEGES FOR ROLE catan_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO catan_app;

\connect catan_test

REVOKE ALL ON DATABASE catan_test FROM PUBLIC;
GRANT CONNECT ON DATABASE catan_test TO catan_migrator, catan_app;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO catan_app;

ALTER DEFAULT PRIVILEGES FOR ROLE catan_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO catan_app;
ALTER DEFAULT PRIVILEGES FOR ROLE catan_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO catan_app;
