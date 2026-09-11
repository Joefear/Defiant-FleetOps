"""Slice 4 changes only space structures; all prior data and security survive round trips."""

from sqlalchemy import select

from fleetops.db.metadata import (
    actors,
    external_references,
    facilities,
    items,
    locations,
    organizations,
    parties,
    party_roles,
    sessions,
    users,
)
from fleetops.domain.location_kinds import LocationKind

PRIOR_TABLES = (
    organizations,
    actors,
    parties,
    party_roles,
    users,
    sessions,
    items,
    external_references,
)


def prior_snapshot(connection):
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


def test_space_migration_round_trip_preserves_prior_data_and_security(
    database, space_data, migrator_connection
):
    try:
        # Capture the historical Slice 4 schema, before later slices add Item support.
        result = database.migrate("downgrade", "0005_space_cycle_guard")
        assert result.returncode == 0, result.stdout + result.stderr
        before = prior_snapshot(migrator_connection)
        for operation, revision in (
            ("downgrade", "0003_catalog"),
            ("upgrade", "0004_space"),
            ("downgrade", "0003_catalog"),
            ("upgrade", "0004_space"),
        ):
            result = database.migrate(operation, revision)
            assert result.returncode == 0, result.stdout + result.stderr
            assert prior_snapshot(migrator_connection) == before
            expected = {table.name for table in PRIOR_TABLES} | {"alembic_version"}
            if revision == "0004_space":
                expected |= {"facilities", "locations"}
            assert (
                set(
                    migrator_connection.exec_driver_sql(
                        "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
                    ).scalars()
                )
                == expected
            )
            migrator_connection.rollback()
        # Historical round trips still inspect 0004; metadata checks require current head.
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr
        result = database.migrate("check")
        assert result.returncode == 0, result.stdout + result.stderr
        for table in (facilities, locations):
            assert migrator_connection.execute(select(table)).all() == []
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stdout + result.stderr


def test_space_schema_is_exact_and_uses_tenant_safe_keys(migrator_connection):
    assert {kind.value for kind in LocationKind} == {
        "SITE",
        "ROOM",
        "RACK",
        "BIN",
        "STATION",
        "DOCK",
        "VEHICLE",
        "OTHER",
    }
    connection = migrator_connection
    # Make pg_get_constraintdef render qualified targets consistently.
    connection.exec_driver_sql("SET LOCAL search_path = pg_catalog")
    assert set(
        connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
        ).scalars()
    ) == {table.name for table in PRIOR_TABLES} | {
        "alembic_version",
        "facilities",
        "locations",
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
    assert connection.exec_driver_sql(
        "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='fleetops' ORDER BY p.proname"
    ).all() == [
        (name,)
        for name in (
            "assign_asset",
            "change_custody",
            "change_ownership",
            "current_authenticated_actor",
            "enforce_asset_initial_state",
            "enforce_authenticated_creator",
            "enforce_authenticated_updater",
            "enforce_location_acyclic",
            "enforce_purchase_order",
            "enforce_purchase_order_line",
            "issue_session",
            "move_asset",
            "resolve_session",
            "revoke_current_session",
            "transition_asset",
            "unassign_asset",
        )
    ]
    for table in (facilities, locations):
        columns = {
            row.attname: (row.type, row.attnotnull)
            for row in connection.exec_driver_sql(
                "SELECT attname, atttypid::regtype::text AS type, attnotnull "
                "FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 "
                "AND NOT attisdropped",
                (f"fleetops.{table.name}",),
            )
        }
        required = {
            "id": ("uuid", True),
            "org_id": ("uuid", True),
            "name": ("text", True),
            "active": ("boolean", True),
            "created_by_actor_id": ("uuid", True),
            "created_at": ("timestamp with time zone", True),
        }
        required.update(
            {"timezone": ("text", True)}
            if table is facilities
            else {
                "facility_id": ("uuid", True),
                "parent_location_id": ("uuid", False),
                "code": ("text", True),
                "kind": ("text", True),
            }
        )
        assert columns == required
        constraints = dict(
            connection.exec_driver_sql(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=%s::regclass",
                (f"fleetops.{table.name}",),
            ).all()
        )
        assert constraints[f"{table.name}_pkey"] == "PRIMARY KEY (id)"
        assert constraints[f"uq_{table.name}_org_id_id"] == "UNIQUE (org_id, id)"
        assert constraints[f"fk_{table.name}_org"] == (
            "FOREIGN KEY (org_id) REFERENCES fleetops.organizations(id)"
        )
        assert constraints[f"fk_{table.name}_creator"] == (
            "FOREIGN KEY (org_id, created_by_actor_id) REFERENCES fleetops.actors(org_id, id)"
        )
    assert constraints["uq_locations_org_facility_id"] == "UNIQUE (org_id, facility_id, id)"
    assert constraints["uq_locations_facility_code"] == "UNIQUE (org_id, facility_id, code)"
    assert constraints["fk_locations_parent"] == (
        "FOREIGN KEY (org_id, facility_id, parent_location_id) "
        "REFERENCES fleetops.locations(org_id, facility_id, id)"
    )
    assert constraints["fk_locations_facility"] == (
        "FOREIGN KEY (org_id, facility_id) REFERENCES fleetops.facilities(org_id, id)"
    )
