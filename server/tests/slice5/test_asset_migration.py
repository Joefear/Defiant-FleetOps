"""Frozen migration round trips preserve exact prior data, schema and security."""

from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice4.test_space_cycle_migration import guard_objects
from server.tests.slice5.conftest import TABLES
from sqlalchemy import create_engine


def test_fresh_bootstrap_round_trip_restores_pre_0006_state(fresh_database):
    """The start snapshot predates 0006, so a symmetric downgrade bug cannot hide."""
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            assert_fresh_round_trip(fresh_database, connection)
    finally:
        engine.dispose()


def assert_fresh_round_trip(database, migrator_connection):
    head = schema_snapshot(migrator_connection)
    try:
        result = database.migrate("downgrade", "0005_space_cycle_guard")
        assert result.returncode == 0, result.stdout + result.stderr
        assert schema_snapshot(migrator_connection) == database.historical_0005
        assert guard_objects(migrator_connection) == (1, 1, 1)
        assert (
            migrator_connection.exec_driver_sql(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname='fleetops_authenticator'"
            ).scalar_one()
            is True
        )
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        assert schema_snapshot(migrator_connection) == head
        result = database.migrate("check")
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr


def test_asset_migration_round_trip_restores_exact_0005_and_recreates_head(
    database, space_data, migrator_connection
):
    try:
        result = database.migrate("downgrade", "0005_space_cycle_guard")
        assert result.returncode == 0, result.stdout + result.stderr
        before = schema_snapshot(migrator_connection)
        assert guard_objects(migrator_connection) == (1, 1, 1)
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        upgraded = schema_snapshot(migrator_connection)
        assert guard_objects(migrator_connection) == (1, 1, 1)
        result = database.migrate("check")
        assert result.returncode == 0, result.stdout + result.stderr
        result = database.migrate("downgrade", "0005_space_cycle_guard")
        assert result.returncode == 0, result.stdout + result.stderr
        assert schema_snapshot(migrator_connection) == before
        assert guard_objects(migrator_connection) == (1, 1, 1)
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        assert schema_snapshot(migrator_connection) == upgraded
        result = database.migrate("check")
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr


def test_asset_schema_contains_only_authorized_objects_and_text_states(migrator_connection):
    connection = migrator_connection
    assert connection.exec_driver_sql("SHOW server_version").scalar_one().startswith("16.")
    assert set(
        connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
        ).scalars()
    ) == {
        "organizations",
        "actors",
        "parties",
        "party_roles",
        "users",
        "sessions",
        "items",
        "external_references",
        "facilities",
        "locations",
        "alembic_version",
        "assets",
        "asset_identifiers",
        "asset_transitions",
        "asset_initial_facts",
        "asset_movements",
        "asset_custody_changes",
        "asset_ownership_changes",
        "asset_assignment_events",
        "asset_initial_assignment_facts",
        "asset_configurations",
        "purchase_orders",
        "purchase_order_lines",
    }
    assert (
        connection.exec_driver_sql(
            "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON "
            "n.oid=t.typnamespace WHERE n.nspname='fleetops' AND t.typtype='e'"
        ).scalar_one()
        == 0
    )
    for table in TABLES:
        columns = dict(
            connection.exec_driver_sql(
                (
                    "SELECT column_name, data_type FROM information_schema.columns WHERE "
                    "table_schema='fleetops' AND table_name=%s"
                ),
                (table.name,),
            ).all()
        )
        assert set(columns) == set(table.c.keys())
        assert "transition_seq" not in columns
        assert (
            not {
                "state_version",
                "location_version",
                "custody_version",
                "ownership_version",
                "assignment_version",
            }
            & columns.keys()
        )
        for column in ("current_state", "from_state", "to_state", "type"):
            if column in columns:
                assert columns[column] == "text"
