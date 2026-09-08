"""Apply the privileged, versioned role bootstrap before unprivileged Alembic DDL."""

import argparse
import os
from importlib.resources import files

import psycopg
from psycopg import sql


def bootstrap_database(
    connection: psycopg.Connection,
    *,
    migrator_password: str,
    app_password: str,
    authenticator_password: str,
) -> None:
    """Fail closed on reused roles/nonempty databases; never modify an existing installation.

    The privilege domains only separate anything if they are genuinely distinct logins,
    so a shared or empty password is rejected before any SQL runs. Everything below runs
    in one transaction: a half-applied bootstrap would leave roles without their deny
    defaults, which is worse than no bootstrap at all.
    """
    passwords = (migrator_password, app_password, authenticator_password)
    if not all(passwords) or len(set(passwords)) != 3:
        raise ValueError("Distinct, nonempty database role passwords are required")
    with connection.transaction():
        if connection.info.server_version // 10000 != 16:
            raise RuntimeError("STOP: Slice 1 requires real PostgreSQL 16")
        # Roles are cluster-wide. Reusing an existing fleetops_* role would silently
        # inherit whatever grants it already holds elsewhere, so refuse rather than adopt.
        if connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname IN "
            "('fleetops_migrator', 'fleetops_app', 'fleetops_authenticator')"
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
        bootstrap_authenticator(connection, authenticator_password=authenticator_password)


def bootstrap_authenticator(connection: psycopg.Connection, *, authenticator_password: str) -> None:
    """Apply only the forward admin step to an existing 0001-bootstrap installation.

    This path does not rewrite either existing login, grants, or historical bootstrap.
    Deployment must provision an independent secret; role membership is never created.
    """
    if not authenticator_password:
        raise ValueError("A nonempty independent authenticator password is required")
    with connection.transaction():
        if connection.info.server_version // 10000 != 16:
            raise RuntimeError("Bootstrap requires PostgreSQL 16")
        if connection.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='fleetops'"
        ).fetchone() != ("fleetops_migrator",):
            raise RuntimeError("Apply bootstrap_001_roles first")
        script = (
            files("fleetops.db")
            .joinpath("bootstrap_002_authenticator.sql")
            .read_text(encoding="utf-8")
        )
        connection.execute(
            sql.SQL(script).format(
                database=sql.Identifier(connection.info.dbname),
                authenticator_password=sql.Literal(authenticator_password),
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--authenticator-only",
        action="store_true",
        help="Apply bootstrap_002 to an existing bootstrap_001 installation",
    )
    arguments = parser.parse_args()
    with psycopg.connect(os.environ["FLEETOPS_BOOTSTRAP_URL"]) as connection:
        if arguments.authenticator_only:
            bootstrap_authenticator(
                connection, authenticator_password=os.environ["FLEETOPS_AUTHENTICATOR_PASSWORD"]
            )
        else:
            bootstrap_database(
                connection,
                migrator_password=os.environ["FLEETOPS_MIGRATOR_PASSWORD"],
                app_password=os.environ["FLEETOPS_APP_PASSWORD"],
                authenticator_password=os.environ["FLEETOPS_AUTHENTICATOR_PASSWORD"],
            )
    print(
        "Applied bootstrap_002_authenticator"
        if arguments.authenticator_only
        else "Applied bootstrap_001_roles and bootstrap_002_authenticator"
    )


if __name__ == "__main__":
    main()
