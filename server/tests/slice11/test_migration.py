"""Exact pre-evidence restoration and refusal to erase immutable captured evidence."""

from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice11.conftest import upload
from sqlalchemy import create_engine


def test_exact_0011_round_trip_and_metadata(fresh_database):
    """A pre-0012 snapshot prevents a symmetric upgrade/downgrade bug hiding itself."""
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            current = schema_snapshot(connection)
            roles = role_snapshot(connection)
            migrate(fresh_database, "check")
            for _ in range(2):
                migrate(fresh_database, "downgrade", "0011_corrections")
                assert schema_snapshot(connection) == fresh_database.historical_0011
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "upgrade", "head")
                assert schema_snapshot(connection) == current
                migrate(fresh_database, "check")
    finally:
        migrate(fresh_database, "upgrade", "head")
        engine.dispose()


def test_downgrade_refuses_captured_evidence_atomically(
    database, evidence_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    capture = upload(evidence_client, tenant)
    assert capture.status_code == 201
    before = schema_snapshot(migrator_connection)
    failed = database.migrate("downgrade", "0011_corrections")
    assert failed.returncode != 0
    assert "Cannot downgrade accepted evidence" in failed.stderr
    assert schema_snapshot(migrator_connection) == before
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT version_num FROM fleetops.alembic_version"
        ).scalar_one()
        == "0012_evidence"
    )
