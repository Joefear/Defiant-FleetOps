"""Slice 1 acceptance: Alembic round-trips cleanly and only the DDL owner may run it."""

from conftest import assert_migration_succeeded, user_relations


def test_alembic_upgrade_downgrade_round_trip(database, migrator_connection):
    # Alembic's own downgrade leaves the empty bookkeeping table behind by design, so the
    # table is dropped by hand to reproduce a genuinely empty database before the upgrade
    # that acceptance requires. Upgrading twice proves idempotence; "check" proves the
    # declared metadata and the live schema agree, which is how a leaked domain table
    # would surface.
    try:
        assert_migration_succeeded(database.migrate("downgrade", "base"))
        assert (
            migrator_connection.exec_driver_sql(
                "SELECT version_num FROM fleetops.alembic_version"
            ).all()
            == []
        )
        assert user_relations(migrator_connection) == [("fleetops", "alembic_version", "r")]
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql("DROP TABLE fleetops.alembic_version")
        migrator_connection.commit()
        assert user_relations(migrator_connection) == []
        migrator_connection.rollback()
        assert_migration_succeeded(database.migrate("upgrade", "head"))
        assert_migration_succeeded(database.migrate("upgrade", "head"))
        assert (
            migrator_connection.exec_driver_sql(
                "SELECT version_num FROM fleetops.alembic_version"
            ).scalar_one()
            == "0007_asset_fact_history"
        )
        migrator_connection.rollback()
        assert_migration_succeeded(database.migrate("check"))
    finally:
        migrator_connection.rollback()
        assert_migration_succeeded(database.migrate("upgrade", "head"))


def test_alembic_refuses_application_credentials(database):
    # Failure mode guarded: a migration run as the runtime role would create app-owned
    # objects, and an owner can grant itself anything. Ownership is the whole model.
    result = database.migrate("upgrade", "head", role="fleetops_app")
    assert result.returncode != 0
    assert (
        "Migrations require postgresql+psycopg and fleetops_migrator credentials" in result.stderr
    )
