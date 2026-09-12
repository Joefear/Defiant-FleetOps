"""Real PostgreSQL 16 only: one disposable cluster and fresh database per pytest session."""

import os
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from server.tests.migration_snapshot import schema_snapshot
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from fleetops.db.bootstrap import bootstrap_database

ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = (
    "postgres:16.15@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
)


def docker(*arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=60, check=True, env=env
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class Database:
    port: int
    name: str
    migrator_password: str = field(repr=False)
    app_password: str = field(repr=False)
    authenticator_password: str = field(repr=False)
    historical_0005: dict = field(default_factory=dict, repr=False, compare=False)
    historical_0006: dict = field(default_factory=dict, repr=False, compare=False)
    historical_0007: dict = field(default_factory=dict, repr=False, compare=False)
    historical_0008: dict = field(default_factory=dict, repr=False, compare=False)
    historical_0009: dict = field(default_factory=dict, repr=False, compare=False)

    def url(self, role: str) -> URL:
        passwords = {
            "fleetops_migrator": self.migrator_password,
            "fleetops_app": self.app_password,
            "fleetops_authenticator": self.authenticator_password,
        }
        return URL.create(
            "postgresql+psycopg",
            username=role,
            password=passwords[role],
            host="127.0.0.1",
            port=self.port,
            database=self.name,
        )

    def settings(self, organization_id):
        """Configure both independently authenticated runtime domains for the real API."""
        from fleetops.settings import Settings

        return Settings(
            self.url("fleetops_app"), organization_id, self.url("fleetops_authenticator")
        )

    def migrate(
        self, *arguments: str, role: str = "fleetops_migrator"
    ) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["FLEETOPS_MIGRATOR_URL"] = self.url(role).render_as_string(hide_password=False)
        return subprocess.run(
            [sys.executable, "-m", "alembic", "-c", str(ROOT / "alembic.ini"), *arguments],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


def assert_migration_succeeded(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def user_relations(connection):
    return connection.exec_driver_sql(
        "SELECT n.nspname, c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname NOT IN ('information_schema') AND left(n.nspname, 3) <> 'pg_' "
        "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f') ORDER BY 1, 2"
    ).all()


@pytest.fixture(scope="session")
def database():
    yield from disposable_database()


@pytest.fixture
def fresh_database():
    """A second clean cluster when a proof must predate every historical migration."""
    yield from disposable_database()


def disposable_database():
    """No external DB fallback, no SQLite, no skips; clean up only this session's container.

    The container binds a random localhost-only port so it never collides with (or gets
    mistaken for) a developer's own PostgreSQL, keeps its data on tmpfs so nothing survives
    the session, and forces SCRAM for host connections so the harness exercises the same
    authentication path production will. --rm plus the explicit stop in the finally block
    is deliberate redundancy: a leaked cluster full of test roles is not a fixture.
    """
    name = f"fleetops-test-{uuid4().hex}"
    env = os.environ.copy()
    env["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
    started = False
    try:
        try:
            docker(
                "run",
                "--detach",
                "--rm",
                "--name",
                name,
                "--label",
                "fleetops.test-session=true",
                "--publish",
                "127.0.0.1::5432",
                "--tmpfs",
                "/var/lib/postgresql/data",
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256",
                POSTGRES_IMAGE,
                env=env,
            )
            started = True
            port = int(docker("port", name, "5432/tcp").rsplit(":", 1)[1])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            pytest.fail(f"STOP: cannot provide real PostgreSQL 16 via Docker: {error}")
        admin_options = {
            "host": "127.0.0.1",
            "port": port,
            "user": "postgres",
            "password": env["POSTGRES_PASSWORD"],
            "dbname": "postgres",
            "connect_timeout": 2,
        }
        deadline = time.monotonic() + 45
        while True:
            try:
                admin = psycopg.connect(**admin_options, autocommit=True)
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("STOP: PostgreSQL 16 did not become ready within 45 seconds")
                time.sleep(0.25)
        db = Database(
            port,
            f"fleetops_test_{uuid4().hex}",
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
            secrets.token_urlsafe(32),
        )
        with admin:
            assert admin.info.server_version // 10000 == 16, "STOP: PostgreSQL 16 required"
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(db.name))
            )
        admin_options["dbname"] = db.name
        with psycopg.connect(**admin_options) as bootstrap:
            bootstrap_database(
                bootstrap,
                migrator_password=db.migrator_password,
                app_password=db.app_password,
                authenticator_password=db.authenticator_password,
            )
        # Capture a genuinely fresh historical boundary before 0006 has ever run.
        # Alembic does not own the separately bootstrapped authenticator role.
        assert_migration_succeeded(db.migrate("upgrade", "0005_space_cycle_guard"))
        snapshot_engine = create_engine(db.url("fleetops_migrator"), poolclass=NullPool)
        try:
            with snapshot_engine.connect() as connection:
                db.historical_0005.update(schema_snapshot(connection))
            # Capture 0006 before the physical-fact migration has ever run, so an
            # incomplete downgrade cannot validate itself through a symmetric bug.
            assert_migration_succeeded(db.migrate("upgrade", "0006_assets"))
            with snapshot_engine.connect() as connection:
                db.historical_0006.update(schema_snapshot(connection))
            assert_migration_succeeded(db.migrate("upgrade", "0007_asset_fact_history"))
            with snapshot_engine.connect() as connection:
                db.historical_0007.update(schema_snapshot(connection))
            assert_migration_succeeded(db.migrate("upgrade", "0008_assignment_configuration"))
            with snapshot_engine.connect() as connection:
                db.historical_0008.update(schema_snapshot(connection))
            # Capture procurement before receiving has ever existed in this cluster.
            assert_migration_succeeded(db.migrate("upgrade", "0009_procurement"))
            with snapshot_engine.connect() as connection:
                db.historical_0009.update(schema_snapshot(connection))
        finally:
            snapshot_engine.dispose()
        assert_migration_succeeded(db.migrate("upgrade", "head"))
        yield db
        # Every test-only table, sequence, view and function must have been destroyed.
        engine = create_engine(db.url("fleetops_migrator"), poolclass=NullPool)
        try:
            with engine.connect() as connection:
                assert user_relations(connection) == sorted(
                    [
                        ("fleetops", name, "r")
                        for name in (
                            "actors",
                            "alembic_version",
                            "asset_assignment_events",
                            "asset_configurations",
                            "asset_custody_changes",
                            "asset_identifiers",
                            "asset_initial_facts",
                            "asset_initial_assignment_facts",
                            "asset_movements",
                            "asset_ownership_changes",
                            "asset_transitions",
                            "assets",
                            "external_references",
                            "facilities",
                            "items",
                            "locations",
                            "organizations",
                            "parties",
                            "party_roles",
                            "purchase_orders",
                            "purchase_order_lines",
                            "receipts",
                            "receipt_comparators",
                            "receipt_lines",
                            "receipt_reconciliations",
                            "receiving_exceptions",
                            "sessions",
                            "users",
                        )
                    ]
                    + [("fleetops", "asset_configurations_configuration_seq_seq", "S")]
                )
                assert connection.exec_driver_sql(
                    "SELECT p.proname FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname NOT IN ('information_schema') "
                    "AND left(n.nspname, 3) <> 'pg_' ORDER BY p.proname"
                ).all() == [
                    ("assign_asset",),
                    ("change_custody",),
                    ("change_ownership",),
                    ("create_received_unit",),
                    ("current_authenticated_actor",),
                    ("enforce_asset_initial_state",),
                    ("enforce_authenticated_creator",),
                    ("enforce_authenticated_updater",),
                    ("enforce_location_acyclic",),
                    ("enforce_purchase_order",),
                    ("enforce_purchase_order_line",),
                    ("enforce_receiving_completeness",),
                    ("enforce_receiving_exception",),
                    ("enforce_receiving_record",),
                    ("issue_session",),
                    ("lock_receiving_context",),
                    ("move_asset",),
                    ("resolve_session",),
                    ("revoke_current_session",),
                    ("transition_asset",),
                    ("unassign_asset",),
                ]
        finally:
            engine.dispose()
    finally:
        if started:
            docker("stop", "--time", "5", name)


@pytest.fixture
def migrator_connection(database):
    """Fresh DDL-owner connection per test; lock_timeout keeps a stuck test from hanging CI.

    Permission tests routinely leave the app connection mid-transaction. Its locks are held
    until rollback, so DDL from this connection fails fast instead of waiting forever.
    """
    engine = create_engine(database.url("fleetops_migrator"), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET lock_timeout = '5s'")
            connection.commit()
            yield connection
    finally:
        engine.dispose()


@pytest.fixture
def app_connection(database):
    """Fresh runtime-role connection per test. Every denial proved here is a real one."""
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


@pytest.fixture
def throwaway_table(migrator_connection, app_connection):
    """Migrator-owned probe table with one row, dropped after the test.

    Named per test so concurrent or repeated runs never collide inside the shared session
    database. Both connections are rolled back before DROP: SQLAlchemy autobegins on the
    first statement, and an open app transaction still holds its lock on the probe table
    even after a denied statement, which would block the drop until lock_timeout.
    """
    table = f"slice1_probe_{uuid4().hex}"
    qualified = f'fleetops."{table}"'
    migrator_connection.exec_driver_sql(
        f"CREATE TABLE {qualified} (id integer PRIMARY KEY, value integer NOT NULL)"
    )
    migrator_connection.exec_driver_sql(f"INSERT INTO {qualified} VALUES (1, 10)")
    migrator_connection.commit()
    try:
        yield table, qualified
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(f"DROP TABLE {qualified}")
        migrator_connection.commit()


@pytest.fixture
def authenticator_connection(database):
    """Actual independent login-only role, never SET ROLE from an app connection."""
    engine = create_engine(
        database.url("fleetops_authenticator"), poolclass=NullPool, hide_parameters=True
    )
    try:
        with engine.connect() as connection:
            yield connection
    finally:
        engine.dispose()


@pytest.fixture
def assert_denied():
    """Assert a statement fails with SQLSTATE 42501 (insufficient_privilege), nothing else.

    A generic "raises" would also accept a typo in a table name, and a permission test that
    passes because the object does not exist proves the opposite of what it claims.
    """

    def denied(connection, statement):
        try:
            with pytest.raises(DBAPIError) as error:
                connection.exec_driver_sql(statement)
            assert error.value.orig.sqlstate == "42501", "Expected insufficient_privilege"
        finally:
            connection.rollback()

    return denied
