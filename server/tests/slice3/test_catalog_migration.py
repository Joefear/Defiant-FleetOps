"""Slice 3 changes only catalog structures; Slice 2 data and security survive round trips."""

from sqlalchemy import select

from fleetops.db.metadata import (
    actors,
    external_references,
    items,
    organizations,
    parties,
    party_roles,
    sessions,
    users,
)

PRIOR_TABLES = (organizations, actors, parties, party_roles, users, sessions)


def slice2_snapshot(connection):
    """Compare data, constraints, policies, ownership, column ACLs, and the resolver definition."""
    result = {}
    for table in PRIOR_TABLES:
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
                "WHERE conrelid=%s::regclass ORDER BY conname",
                (qualified,),
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


def test_catalog_migration_round_trip_preserves_slice2(database, catalog_data, migrator_connection):
    head_before = slice2_snapshot(migrator_connection)
    try:
        # ADR-006 intentionally changes authentication ACLs at 0006. Compare the
        # unchanged Slice 2 contract across 0002/0003 from historical 0005, then
        # separately prove that returning to head restores the corrected authority.
        result = database.migrate("downgrade", "0005_space_cycle_guard")
        assert result.returncode == 0, result.stderr
        before = slice2_snapshot(migrator_connection)
        for table in PRIOR_TABLES:
            assert before[table.name]["rows"] == head_before[table.name]["rows"]
        result = database.migrate("downgrade", "0002_identity_auth")
        assert result.returncode == 0, result.stderr
        assert set(
            migrator_connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
            ).scalars()
        ) == {table.name for table in PRIOR_TABLES} | {"alembic_version"}
        migrator_connection.rollback()
        assert slice2_snapshot(migrator_connection) == before
        for operation, revision in (
            ("upgrade", "0003_catalog"),
            ("downgrade", "0002_identity_auth"),
            ("upgrade", "0003_catalog"),
        ):
            result = database.migrate(operation, revision)
            assert result.returncode == 0, result.stderr
            assert slice2_snapshot(migrator_connection) == before
        # Compare runtime metadata only after restoring the current head.
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr
        assert slice2_snapshot(migrator_connection) == head_before
        result = database.migrate("check")
        assert result.returncode == 0, result.stderr
        for table in (items, external_references):
            assert migrator_connection.execute(select(table)).all() == []
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr


def test_catalog_schema_has_only_opened_tables_and_internal_primary_keys(
    database, migrator_connection
):
    # Inspect the historical Slice 3 boundary without broadening its original assertions.
    result = database.migrate("downgrade", "0003_catalog")
    assert result.returncode == 0, result.stderr
    try:
        connection = migrator_connection
        assert set(
            connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
            ).scalars()
        ) == {
            "alembic_version",
            "organizations",
            "actors",
            "parties",
            "party_roles",
            "users",
            "sessions",
            "items",
            "external_references",
        }
        assert connection.exec_driver_sql(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='fleetops'"
        ).all() == [("resolve_session",)]
        for table in (items, external_references):
            constraints = dict(
                connection.exec_driver_sql(
                    "SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid=%s::regclass AND contype='p'",
                    (f"fleetops.{table.name}",),
                ).all()
            )
            assert constraints == {"p": "PRIMARY KEY (id)"}
            assert (
                connection.exec_driver_sql(
                    "SELECT attnotnull FROM pg_attribute "
                    "WHERE attrelid=%s::regclass AND attname='org_id'",
                    (f"fleetops.{table.name}",),
                ).scalar_one()
                is True
            )
        assert connection.exec_driver_sql(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='fleetops.items'::regclass AND conname='uq_items_catalog_entry'"
        ).scalar_one() == (
            "UNIQUE NULLS NOT DISTINCT (org_id, manufacturer_party_id, "
            "manufacturer_part_number, revision)"
        )
        uniques = set(
            connection.exec_driver_sql(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid='fleetops.external_references'::regclass AND contype='u'"
            ).scalars()
        )
        assert uniques == {
            "UNIQUE (org_id, id)",
            "UNIQUE (org_id, entity_type, entity_id, system, reference_type, external_value)",
        }
        assert (
            connection.exec_driver_sql(
                "SELECT indisunique FROM pg_index "
                "WHERE indexrelid='fleetops.ix_external_references_search'::regclass"
            ).scalar_one()
            is False
        )
        assert (
            connection.exec_driver_sql(
                "SELECT atttypid::regtype::text FROM pg_attribute "
                "WHERE attrelid='fleetops.items'::regclass AND attname='uom'"
            ).scalar_one()
            == "text"
        )
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr
