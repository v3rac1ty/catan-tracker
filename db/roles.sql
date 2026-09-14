-- Least-privilege role & database bootstrap for Catan Tracker.
--
-- Run once by the `postgres` superuser (via docker-entrypoint-initdb.d, see
-- db/init/01-roles.sh, or manually with `psql`). Idempotent: safe to re-run.
--
-- Expects two psql variables to be passed in with -v:
--   migrator_password  -- password for the catan_migrator (DDL/owner) role
--   app_password        -- password for the catan_app (runtime, least-privilege) role
--
-- Two roles:
--   catan_migrator  owns the schema, runs migrations (DDL).
--   catan_app       runtime role used by the bot: SELECT/INSERT/UPDATE only,
--                   no DELETE/TRUNCATE/DROP/ALTER/CREATE.
--
-- Two databases: `catan` (production) and `catan_test` (integration tests).

\set ON_ERROR_STOP on

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
-- Databases, owned by the migrator role (idempotent create-if-not-exists)
-- ---------------------------------------------------------------------------

SELECT format('CREATE DATABASE catan OWNER catan_migrator')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'catan')
\gexec

SELECT format('CREATE DATABASE catan_test OWNER catan_migrator')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'catan_test')
\gexec

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
