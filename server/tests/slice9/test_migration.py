"""Exact predecessor restoration and explicit installed receiving authority inventory."""

import pytest
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice7.test_migration import sequence_snapshot
from server.tests.slice9.conftest import RECEIVING_TABLES
from sqlalchemy import create_engine, text

INSERTED = {
    "receipts": {
        "id",
        "org_id",
        "vendor_party_id",
        "po_id",
        "dock_location_id",
        "packing_reference",
        "received_at",
    },
    "receipt_comparators": {"id", "org_id", "receipt_id", "po_line_id"},
    "receipt_lines": {
        "id",
        "org_id",
        "receipt_id",
        "po_line_id",
        "item_id",
        "quantity",
        "uom",
        "condition",
        "packing_quantity",
        "notes",
        "owner_party_id",
        "custodian_party_id",
        "observed_identifier_type",
        "observed_identifier_value",
    },
    "receipt_reconciliations": {"id", "org_id", "receipt_id"},
    "receiving_exceptions": {
        "id",
        "org_id",
        "exception_type",
        "receipt_id",
        "receipt_line_id",
        "po_line_id",
        "asset_id",
        "conflicting_asset_id",
    },
}
SERVER_COLUMNS = {
    "receipts": {"vendor_role"},
    "receipt_comparators": set(),
    "receipt_lines": {"serialized", "asset_id"},
    "receipt_reconciliations": set(),
    "receiving_exceptions": set(),
}
FUNCTIONS = {
    "lock_receiving_context",
    "enforce_receiving_record",
    "enforce_receiving_exception",
    "enforce_receiving_completeness",
    "create_received_unit",
}


def assert_exact_additions(before, head):
    """Every preexisting row, constraint, function body, grant and policy is byte-equivalent."""
    assert set(head) == set(before) | set(INSERTED)
    for name in before:
        if name not in {"alembic_version", "functions", "triggers"}:
            assert head[name] == before[name]
    assert set(before["functions"]) < set(head["functions"])
    assert len(head["functions"]) == len(before["functions"]) + 5
    assert set(before["triggers"]) < set(head["triggers"])
    assert len(head["triggers"]) == len(before["triggers"]) + 13
    for name in INSERTED:
        assert head[name]["rows"] == []


def round_trip(database, connection, historical=None):
    head = schema_snapshot(connection)
    roles, sequence = role_snapshot(connection), sequence_snapshot(connection)
    try:
        migrate(database, "downgrade", "0009_procurement")
        before = schema_snapshot(connection)
        if historical is not None:
            assert before == historical
        migrate(database, "upgrade", "0010_receiving")
        assert schema_snapshot(connection) == head
        assert_exact_additions(before, head)
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
        migrate(database, "check")
        migrate(database, "downgrade", "0009_procurement")
        assert schema_snapshot(connection) == before
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
        migrate(database, "upgrade", "0010_receiving")
        assert schema_snapshot(connection) == head
        migrate(database, "check")
    finally:
        connection.rollback()
        migrate(database, "upgrade", "head")


def test_fresh_0009_to_0010_round_trip_exactly_restores_all_catalogs(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            round_trip(fresh_database, connection, fresh_database.historical_0009)
    finally:
        engine.dispose()


def test_populated_0009_to_0010_round_trip_preserves_procurement_and_asset_history(
    database, asset_data, make_order, migrator_connection
):
    make_order()
    round_trip(database, migrator_connection)


@pytest.mark.parametrize("table", RECEIVING_TABLES, ids=lambda t: t.name)
def test_schema_has_exact_fields_text_checks_rls_and_minimum_column_grants(
    table, migrator_connection
):
    c, name = migrator_connection, table.name
    qualified = f"fleetops.{name}"
    columns = c.execute(
        text("""
        SELECT column_name, data_type, is_nullable, numeric_scale FROM information_schema.columns
        WHERE table_schema='fleetops' AND table_name=:name
    """),
        {"name": name},
    ).all()
    expected = INSERTED[name] | SERVER_COLUMNS[name] | {"actor_id", "occurred_at", "recorded_at"}
    assert {row.column_name for row in columns} == expected
    assert next(row for row in columns if row.column_name == "org_id").is_nullable == "NO"
    for row in columns:
        if row.column_name in {"condition", "exception_type", "uom", "observed_identifier_type"}:
            assert row.data_type == "text"
        if row.column_name in {"quantity", "packing_quantity"}:
            assert (row.data_type, row.numeric_scale) == ("numeric", None)
    assert c.execute(
        text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:q AS regclass)"), {"q": qualified}
    ).scalar_one()
    policies = c.execute(
        text("""
        SELECT permissive, roles, cmd, qual, with_check FROM pg_policies
        WHERE schemaname='fleetops' AND tablename=:name ORDER BY policyname
    """),
        {"name": name},
    ).all()
    assert [row.permissive for row in policies] == ["PERMISSIVE", "RESTRICTIVE"]
    assert all(
        row.roles == ["fleetops_app"]
        and row.cmd == "ALL"
        and row.qual == row.with_check
        and "NULLIF" in row.qual
        and "org_id" in row.qual
        for row in policies
    )
    for role in ["fleetops_app", "fleetops_authenticator"]:
        for privilege in [
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ]:
            assert c.execute(
                text("SELECT has_table_privilege(:r,:q,:p)"),
                {"r": role, "q": qualified, "p": privilege},
            ).scalar_one() is (role == "fleetops_app" and privilege == "SELECT")
        for field in expected:
            for privilege in ["INSERT", "UPDATE", "REFERENCES"]:
                allowed = (privilege == "INSERT" and field in INSERTED[name]) or (
                    privilege == "UPDATE" and name == "receipts" and field == "id"
                )
                assert c.execute(
                    text("SELECT has_column_privilege(:r,:q,:f,:p)"),
                    {"r": role, "q": qualified, "f": field, "p": privilege},
                ).scalar_one() is (role == "fleetops_app" and allowed)
    assert (
        c.execute(
            text("""
        SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid=CAST(:q AS regclass) AND a.grantee=0
    """),
            {"q": qualified},
        ).scalar_one()
        == 0
    )


def test_exact_five_functions_have_one_narrow_receipt_owned_definer(migrator_connection):
    c = migrator_connection
    rows = (
        c.execute(
            text("""
        SELECT p.proname, pg_get_function_identity_arguments(p.oid) arguments,
               p.prosecdef, p.proconfig, pg_get_userbyid(p.proowner) owner,
               has_function_privilege('fleetops_app',p.oid,'EXECUTE') app,
               has_function_privilege('fleetops_authenticator',p.oid,'EXECUTE') auth,
               pg_get_functiondef(p.oid) definition,
               EXISTS (SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0) public_acl
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops' AND p.proname=ANY(:names) ORDER BY p.proname
    """),
            {"names": list(FUNCTIONS)},
        )
        .mappings()
        .all()
    )
    assert {row["proname"] for row in rows} == FUNCTIONS
    for row in rows:
        assert row["owner"] == "fleetops_migrator"
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        assert row["prosecdef"] is (row["proname"] == "create_received_unit")
        assert row["app"] is (row["proname"] in {"create_received_unit", "lock_receiving_context"})
        assert not row["auth"] and not row["public_acl"]
        if row["proname"] == "create_received_unit":
            assert "p_org" not in row["arguments"] and "p_actor" not in row["arguments"]
            assert row["arguments"].startswith("p_receipt_id uuid, p_line_id uuid")
            assert "current_authenticated_actor()" in row["definition"]
            assert "EXCEPTION WHEN" not in row["definition"]


def test_no_enum_conversion_fulfillment_or_deferred_receiving_objects(migrator_connection):
    c = migrator_connection
    assert (
        c.exec_driver_sql("""
        SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='fleetops' AND t.typtype='e'
    """).all()
        == []
    )
    names = (
        c.exec_driver_sql("""
        SELECT table_name || '.' || column_name FROM information_schema.columns
        WHERE table_schema='fleetops'
        UNION ALL
        SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops'
    """)
        .scalars()
        .all()
    )
    prohibited = [
        "conversion",
        "normalized_quantity",
        "equivalent_quantity",
        "canonical_uom",
        "normalized_uom",
        "units_per_package",
        "remaining_quantity",
        "open_quantity",
        "fulfillment",
        "received_to_date",
        "supplier_completion",
        "stock_ledger",
        "material_lots",
        "shipment_schedule",
    ]
    assert not [(name, token) for name in names for token in prohibited if token in name.lower()]
    constraints = c.exec_driver_sql("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid IN
            ('fleetops.receipt_lines'::regclass, 'fleetops.receiving_exceptions'::regclass)
    """).all()
    assert any(
        name == "ck_receipt_lines_condition"
        and all(token in definition for token in ["GOOD", "DAMAGED", "OPENED", "UNKNOWN"])
        for name, definition in constraints
    )
    assert any(
        name == "ck_receiving_exceptions_type"
        and all(
            token in definition
            for token in [
                "SHORT",
                "OVER",
                "SUBSTITUTION",
                "DAMAGED",
                "OPENED",
                "SERIAL_UNREADABLE",
                "SERIAL_MISMATCH",
                "UNEXPECTED_ITEM",
                "QUANTITY_VARIANCE",
                "UOM_MISMATCH",
            ]
        )
        for name, definition in constraints
    )
