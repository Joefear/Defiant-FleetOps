"""Restore the independently captured pre-capture schema; refuse deleting accepted records."""

from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice13.conftest import operation, submit
from sqlalchemy import create_engine


def test_exact_0013_restore_and_metadata(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            current = schema_snapshot(connection)
            roles = role_snapshot(connection)
            migrate(fresh_database, "check")
            for _ in range(2):
                migrate(fresh_database, "downgrade", "0013_labels")
                assert schema_snapshot(connection) == fresh_database.historical_0013
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "upgrade", "head")
                assert schema_snapshot(connection) == current
                migrate(fresh_database, "check")
    finally:
        migrate(fresh_database, "upgrade", "head")
        engine.dispose()


def test_accepted_capture_prevents_downgrade(
    capture_client, asset_data, database, migrator_connection
):
    submit(capture_client, asset_data[0], [operation(asset_data[0])])
    before = schema_snapshot(migrator_connection)
    failed = database.migrate("downgrade", "0013_labels")
    assert failed.returncode != 0 and "Cannot downgrade accepted capture records" in failed.stderr
    assert schema_snapshot(migrator_connection) == before
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT version_num FROM fleetops.alembic_version"
        ).scalar_one()
        == "0014_capture"
    )
