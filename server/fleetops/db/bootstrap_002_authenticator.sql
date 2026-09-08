-- Forward administrative bootstrap for ADR-006. The migrator cannot create roles.
-- Independent login authority is never reachable through app-role membership.
CREATE ROLE fleetops_authenticator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
    NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {authenticator_password};
GRANT CONNECT ON DATABASE {database} TO fleetops_authenticator;
GRANT USAGE ON SCHEMA fleetops TO fleetops_authenticator;
-- bootstrap_001 already withholds PUBLIC TEMP and schema CREATE. Object grants
-- belong to Alembic; this role has no table/function authority until explicitly given it.
ALTER ROLE fleetops_authenticator IN DATABASE {database} SET search_path = pg_catalog;
ALTER ROLE fleetops_authenticator IN DATABASE {database} SET timezone = 'UTC';
