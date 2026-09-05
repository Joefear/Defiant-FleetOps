-- Administrative bootstrap migration, applied once on a new dedicated cluster/database.
-- Placeholders are composed with psycopg.sql identifiers/literals.
--
-- This runs under administrative credentials instead of inside Alembic because creating
-- roles needs CREATEROLE, which fleetops_migrator deliberately never holds: the DDL owner
-- must not be able to mint new logins or widen its own authority. NOINHERIT means neither
-- role can quietly pick up privileges through some future membership grant; everything a
-- FleetOps role can do must be visible as an explicit GRANT somewhere in this repository.
CREATE ROLE fleetops_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {migrator_password};
CREATE ROLE fleetops_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {app_password};
-- PostgreSQL hands CONNECT and TEMP on every new database to PUBLIC. TEMP is withheld
-- from the app role on purpose: temporary tables and pg_temp functions are the classic
-- way an unprivileged role smuggles its own objects into a search path.
REVOKE ALL PRIVILEGES ON DATABASE {database} FROM PUBLIC;
GRANT CONNECT ON DATABASE {database} TO fleetops_migrator, fleetops_app;
-- Nothing FleetOps lives in public. Closing it means every object has a deliberate home.
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
-- The migrator owns the schema so every later table has one unambiguous owner, and the
-- runtime role gets USAGE only: it may resolve names in the schema but never create
-- there. A runtime role that can create its own tables can route around every grant
-- pattern the migrations apply, so this is the floor the history-table rules stand on.
CREATE SCHEMA fleetops AUTHORIZATION fleetops_migrator;
REVOKE ALL PRIVILEGES ON SCHEMA fleetops FROM PUBLIC;
GRANT USAGE ON SCHEMA fleetops TO fleetops_app;

-- Default privileges for objects the migrator creates later. Unless told otherwise,
-- PostgreSQL grants EXECUTE on new functions and USAGE on new types to PUBLIC, which
-- would make every future SECURITY DEFINER function callable by the app role before any
-- migration granted it. Tables, sequences and schemas are listed as well so the posture
-- reads as one statement of intent rather than relying on the reader knowing which
-- object types PostgreSQL happens to leave closed by default.
-- No IN SCHEMA clause: per-schema defaults are added to the global ones and cannot
-- revoke them, so only a database-wide default actually removes PUBLIC EXECUTE.
ALTER DEFAULT PRIVILEGES FOR ROLE fleetops_migrator
    REVOKE ALL PRIVILEGES ON TABLES FROM PUBLIC, fleetops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fleetops_migrator
    REVOKE ALL PRIVILEGES ON SEQUENCES FROM PUBLIC, fleetops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fleetops_migrator
    REVOKE ALL PRIVILEGES ON FUNCTIONS FROM PUBLIC, fleetops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fleetops_migrator
    REVOKE ALL PRIVILEGES ON TYPES FROM PUBLIC, fleetops_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fleetops_migrator
    REVOKE ALL PRIVILEGES ON SCHEMAS FROM PUBLIC, fleetops_app;

-- Role search_path is a convenience for humans and the application, not a security
-- boundary; privileged functions pin their own (see the SECURITY DEFINER test). Alembic
-- overrides it with a catalog-only path so migration DDL must name the schema explicitly.
-- UTC everywhere: D11 stores every timestamp in UTC and treats local time as a facility
-- property, so no FleetOps session may ever see a server-local clock.
ALTER ROLE fleetops_migrator IN DATABASE {database} SET search_path = fleetops, pg_catalog;
ALTER ROLE fleetops_app IN DATABASE {database} SET search_path = fleetops, pg_catalog;
ALTER ROLE fleetops_migrator IN DATABASE {database} SET timezone = 'UTC';
ALTER ROLE fleetops_app IN DATABASE {database} SET timezone = 'UTC';
