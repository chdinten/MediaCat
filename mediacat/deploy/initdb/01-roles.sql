-- 01-roles.sql
-- Run once by the postgres entrypoint on first boot.
-- Creates three roles with the principle of least privilege:
--   mediacat_migrator — owns schemas, runs DDL (Alembic)
--   mediacat_app      — DML only (the application)
--   mediacat_readonly  — SELECT only (dashboards, exports)
--
-- The superuser password is in the docker secret; the app password
-- is in a separate secret.  Both are read from /run/secrets/.

\set ON_ERROR_STOP on

-- Read app password from file (docker secret).
-- psql \set does not expand env vars, so we use a DO block.
DO $$
DECLARE
    _app_pass text;
BEGIN
    -- The password is set via ALTER ROLE below; read from the mapped secret.
    -- In the entrypoint context the superuser is already authenticated, so
    -- we just need the password string for the app role.
    --
    -- For robustness we accept that the password might already be set
    -- (idempotent re-runs).
    _app_pass := trim(both E'\n' from pg_read_file('/run/secrets/postgres_app_password'));

    -- Migrator: owns the schema, executes DDL via Alembic.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mediacat_migrator') THEN
        EXECUTE format('CREATE ROLE mediacat_migrator LOGIN PASSWORD %L', _app_pass);
    END IF;

    -- App: DML only, used by the running application.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mediacat_app') THEN
        EXECUTE format('CREATE ROLE mediacat_app LOGIN PASSWORD %L', _app_pass);
    END IF;

    -- Readonly: SELECT only.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mediacat_readonly') THEN
        EXECUTE format('CREATE ROLE mediacat_readonly LOGIN PASSWORD %L', _app_pass);
    END IF;
END
$$;

-- Grant connect
GRANT CONNECT ON DATABASE mediacat TO mediacat_migrator;
GRANT CONNECT ON DATABASE mediacat TO mediacat_app;
GRANT CONNECT ON DATABASE mediacat TO mediacat_readonly;

-- Schema ownership: migrator owns public and any future schemas.
ALTER SCHEMA public OWNER TO mediacat_migrator;
GRANT USAGE ON SCHEMA public TO mediacat_app;
GRANT USAGE ON SCHEMA public TO mediacat_readonly;

-- Default privileges so future tables created by migrator are accessible.
ALTER DEFAULT PRIVILEGES FOR ROLE mediacat_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mediacat_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mediacat_migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO mediacat_readonly;
ALTER DEFAULT PRIVILEGES FOR ROLE mediacat_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO mediacat_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mediacat_migrator IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO mediacat_readonly;

-- Revoke public schema create from everyone except migrator.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CREATE ON SCHEMA public TO mediacat_migrator;

-- Extensions that the migrator will need.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "btree_gist";
