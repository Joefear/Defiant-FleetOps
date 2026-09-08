"""Corrective migration preserves committed tables and removes its objects on downgrade."""

from server.tests.auth_context import set_authenticated
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import locations, metadata


def committed_snapshot(connection):
    """Compare data, constraints, policies, ownership, column ACLs, and the resolver definition."""
    result = {}
    # This proof runs at historical 0004/0005, before Asset tables exist.
    historical = {
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
    }
    for table in (table for table in metadata.sorted_tables if table.name in historical):
        qualified = f"fleetops.{table.name}"
        result[table.name] = {
            "rows": connection.execute(select(table).order_by(table.c.id)).all(),
            "table": connection.exec_driver_sql(
                "SELECT pg_get_userbyid(relowner), relrowsecurity, relacl::text "
                "FROM pg_class WHERE oid=%s::regclass",
                (qualified,),
            ).one(),
            "columns": connection.exec_driver_sql(
                "SELECT attname, attnotnull, attacl::text, attgenerated, atttypid::regtype::text "
                "FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped "
                "ORDER BY attnum",
                (qualified,),
            ).all(),
            "constraints": connection.exec_driver_sql(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=%s::regclass AND conname <> 'ck_locations_acyclic' "
                "ORDER BY conname",
                (qualified,),
            ).all(),
            "indexes": connection.exec_driver_sql(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname='fleetops' AND tablename=%s ORDER BY indexname",
                (table.name,),
            ).all(),
            "policies": connection.exec_driver_sql(
                "SELECT policyname, permissive, roles, cmd, qual, with_check FROM pg_policies "
                "WHERE schemaname='fleetops' AND tablename=%s ORDER BY policyname",
                (table.name,),
            ).all(),
        }
    result["resolver"] = connection.exec_driver_sql(
        "SELECT pg_get_functiondef(oid), proacl::text FROM pg_proc "
        "WHERE oid='fleetops.resolve_session(bytea)'::regprocedure"
    ).one()
    connection.rollback()
    return result


def guard_objects(connection):
    """Inventory every corrective object, including the constraint-trigger catalog row."""
    result = (
        connection.exec_driver_sql(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='fleetops' AND p.proname='enforce_location_acyclic'"
        ).scalar_one(),
        connection.exec_driver_sql(
            "SELECT count(*) FROM pg_trigger WHERE tgrelid='fleetops.locations'::regclass "
            "AND tgname='ck_locations_acyclic'"
        ).scalar_one(),
        connection.exec_driver_sql(
            "SELECT count(*) FROM pg_constraint WHERE conrelid='fleetops.locations'::regclass "
            "AND conname='ck_locations_acyclic'"
        ).scalar_one(),
    )
    connection.rollback()
    return result


def test_cycle_guard_migration_round_trip_preserves_committed_slice4(
    database, space_data, space_values, migrator_connection, app_connection
):
    a, _ = space_data
    try:
        result = database.migrate("downgrade", "0004_space")
        assert result.returncode == 0, result.stdout + result.stderr
        assert guard_objects(migrator_connection) == (0, 0, 0)
        before = committed_snapshot(migrator_connection)
        for operation, revision in (
            ("upgrade", "0005_space_cycle_guard"),
            ("downgrade", "0004_space"),
            ("upgrade", "0005_space_cycle_guard"),
        ):
            result = database.migrate(operation, revision)
            assert result.returncode == 0, result.stdout + result.stderr
            assert committed_snapshot(migrator_connection) == before
            guarded = revision == "0005_space_cycle_guard"
            assert guard_objects(migrator_connection) == ((1, 1, 1) if guarded else (0, 0, 0))
            if not guarded:
                # The historical revision must really restore 0004 behavior. Roll
                # the probe back so exact committed fixture data survives re-upgrade.
                first, second = uuid7(), uuid7()
                transaction = app_connection.begin()
                try:
                    set_authenticated(app_connection, a)
                    rows = [
                        space_values(
                            a, locations, id=first, code="OLD-1", parent_location_id=second
                        ),
                        space_values(
                            a, locations, id=second, code="OLD-2", parent_location_id=first
                        ),
                    ]
                    assert set(
                        app_connection.execute(
                            locations.insert().values(rows).returning(locations.c.id)
                        ).scalars()
                    ) == {first, second}
                finally:
                    transaction.rollback()
                assert committed_snapshot(migrator_connection) == before
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        result = database.migrate("check")
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
