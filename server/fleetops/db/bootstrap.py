"""Apply the privileged, versioned role bootstrap before unprivileged Alembic DDL."""

import os
from importlib.resources import files

import psycopg
from psycopg import sql


def bootstrap_database(
    connection: psycopg.Connection, *, migrator_password: str, app_password: str
) -> None:
    """Fail closed on reused roles/nonempty databases; never modify an existing installation.

    The two-role model only separates anything if the roles are genuinely distinct logins,
    so a shared or empty password is rejected before any SQL runs. Everything below runs
    in one transaction: a half-applied bootstrap would leave roles without their deny
    defaults, which is worse than no bootstrap at all.
    """
    if not migrator_password or not app_password or migrator_password == app_password:
        raise ValueError("Distinct, nonempty database role passwords are required")
    with connection.transaction():
        if connection.info.server_version // 10000 != 16:
            raise RuntimeError("STOP: Slice 1 requires real PostgreSQL 16")
        # Roles are cluster-wide. Reusing an existing fleetops_* role would silently
        # inherit whatever grants it already holds elsewhere, so refuse rather than adopt.
        if connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname IN ('fleetops_migrator', 'fleetops_app')"
        ).fetchone():
            raise RuntimeError("Bootstrap requires fresh roles; use a dedicated new cluster")
        # The deny defaults below only cover objects created after they are set. Any
        # pre-existing user schema or public-schema object would sit outside the posture
        # unaudited, so the database must be empty of both.
        if (
            connection.execute(
                "SELECT 1 FROM pg_namespace "
                "WHERE nspname NOT IN ('public', 'information_schema') "
                "AND left(nspname, 3) <> 'pg_'"
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public'"
            ).fetchone()
        ):
            raise RuntimeError("Bootstrap requires an empty database")
        script = (
            files("fleetops.db").joinpath("bootstrap_001_roles.sql").read_text(encoding="utf-8")
        )
        connection.execute(
            sql.SQL(script).format(
                database=sql.Identifier(connection.info.dbname),
                migrator_password=sql.Literal(migrator_password),
                app_password=sql.Literal(app_password),
            )
        )


def main() -> None:
    with psycopg.connect(os.environ["FLEETOPS_BOOTSTRAP_URL"]) as connection:
        bootstrap_database(
            connection,
            migrator_password=os.environ["FLEETOPS_MIGRATOR_PASSWORD"],
            app_password=os.environ["FLEETOPS_APP_PASSWORD"],
        )
    print("Applied bootstrap_001_roles: roles, schema, explicit connection grants, default deny")


if __name__ == "__main__":
    main()
