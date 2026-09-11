"""Exact 0008 restoration, additive current-head inventory, and installed authority."""

import pytest
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice7.test_migration import sequence_snapshot
from sqlalchemy import create_engine, text

from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders


def assert_only_procurement_added(before, after):
    """Every prior table, row, trigger, function and security definition remains exact."""
    assert set(after) == set(before) | {"purchase_orders", "purchase_order_lines"}
    for name, definition in before.items():
        if name not in {"alembic_version", "functions", "triggers"}:
            assert after[name] == definition
    assert set(before["functions"]) < set(after["functions"])
    assert len(after["functions"]) == len(before["functions"]) + 2
    assert set(before["triggers"]) < set(after["triggers"])
    assert len(after["triggers"]) == len(before["triggers"]) + 6
    assert after["purchase_orders"]["rows"] == after["purchase_order_lines"]["rows"] == []


def test_fresh_0008_snapshot_round_trip_exactly_restores_all_owned_objects(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            head = schema_snapshot(connection)
            roles, sequence = role_snapshot(connection), sequence_snapshot(connection)
            try:
                migrate(fresh_database, "downgrade", "0008_assignment_configuration")
                assert schema_snapshot(connection) == fresh_database.historical_0008
                assert role_snapshot(connection) == roles
                assert sequence_snapshot(connection) == sequence
                migrate(fresh_database, "upgrade", "head")
                assert schema_snapshot(connection) == head
                assert_only_procurement_added(fresh_database.historical_0008, head)
                assert role_snapshot(connection) == roles
                assert sequence_snapshot(connection) == sequence
                migrate(fresh_database, "check")
            finally:
                connection.rollback()
                migrate(fresh_database, "upgrade", "head")
    finally:
        engine.dispose()


def test_populated_0008_round_trip_preserves_asset_histories_and_auth(
    database,
    asset_data,
    migrator_connection,
):
    connection = migrator_connection
    try:
        migrate(database, "downgrade", "0008_assignment_configuration")
        before = schema_snapshot(connection)
        roles, sequence = role_snapshot(connection), sequence_snapshot(connection)
        migrate(database, "upgrade", "head")
        head = schema_snapshot(connection)
        assert_only_procurement_added(before, head)
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
        migrate(database, "check")
        migrate(database, "downgrade", "0008_assignment_configuration")
        assert schema_snapshot(connection) == before
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
        migrate(database, "upgrade", "head")
        assert schema_snapshot(connection) == head
        migrate(database, "check")
    finally:
        connection.rollback()
        migrate(database, "upgrade", "head")


@pytest.mark.parametrize("table", [orders, lines], ids=lambda t: t.name)
def test_installed_schema_rls_and_column_grants_have_no_later_workflow_authority(
    table,
    migrator_connection,
):
    c = migrator_connection
    qualified = f"fleetops.{table.name}"
    common = {
        "id",
        "org_id",
        "created_by_actor_id",
        "updated_by_actor_id",
        "created_at",
        "updated_at",
    }
    business = (
        {
            "vendor_party_id",
            "vendor_role",
            "po_number",
            "status",
            "notes",
            "issued_at",
            "issued_by_actor_id",
        }
        if table is orders
        else {
            "po_id",
            "line_number",
            "item_id",
            "quantity",
            "unit_price",
            "uom",
            "expected_date",
            "supersedes_line_id",
        }
    )
    column_rows = c.execute(
        text("""SELECT column_name, data_type, numeric_scale
        FROM information_schema.columns WHERE table_schema='fleetops' AND table_name=:name"""),
        {"name": table.name},
    ).all()
    assert {r.column_name for r in column_rows} == common | business
    if table is lines:
        assert [
            (r.data_type, r.numeric_scale)
            for r in column_rows
            if r.column_name in {"quantity", "unit_price"}
        ] == [("numeric", None)] * 2
    assert c.execute(
        text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:q AS regclass)"), {"q": qualified}
    ).scalar_one()
    policies = c.execute(
        text("""SELECT permissive, roles, cmd, qual, with_check FROM pg_policies
        WHERE schemaname='fleetops' AND tablename=:name ORDER BY policyname"""),
        {"name": table.name},
    ).all()
    assert [r.permissive for r in policies] == ["PERMISSIVE", "RESTRICTIVE"]
    assert all(
        r.roles == ["fleetops_app"]
        and r.cmd == "ALL"
        and r.qual == r.with_check
        and "NULLIF" in r.qual
        and "org_id" in r.qual
        for r in policies
    )
    inserted = common - {"created_at", "updated_at"}
    inserted |= {"vendor_party_id", "po_number", "notes"} if table is orders else business - {"uom"}
    updated = {"updated_by_actor_id", "updated_at"} | (
        {"vendor_party_id", "po_number", "notes", "status"}
        if table is orders
        else {"item_id", "quantity", "unit_price", "expected_date"}
    )
    for role in ("fleetops_app", "fleetops_authenticator"):
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            assert c.execute(
                text("SELECT has_table_privilege(:role,:table,:priv)"),
                {"role": role, "table": qualified, "priv": privilege},
            ).scalar_one() is (role == "fleetops_app" and privilege == "SELECT")
        for field in common | business:
            for privilege, allowed in (
                ("INSERT", inserted),
                ("UPDATE", updated),
                ("REFERENCES", set()),
            ):
                actual = c.execute(
                    text("SELECT has_column_privilege(:role,:table,:field,:priv)"),
                    {"role": role, "table": qualified, "field": field, "priv": privilege},
                ).scalar_one()
                assert actual is (role == "fleetops_app" and field in allowed)
    assert (
        c.execute(
            text("""SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid=CAST(:q AS regclass) AND a.grantee=0"""),
            {"q": qualified},
        ).scalar_one()
        == 0
    )


def test_predecessor_fk_is_not_deferrable_and_both_uniqueness_indexes_are_partial(
    migrator_connection,
):
    c = migrator_connection
    assert c.exec_driver_sql("""SELECT condeferrable, condeferred FROM pg_constraint
        WHERE conname='fk_purchase_order_lines_predecessor' """).one() == (False, False)
    indexes = c.exec_driver_sql("""SELECT indexname, indexdef FROM pg_indexes
        WHERE schemaname='fleetops' AND indexname IN
        ('uq_purchase_order_lines_root','uq_purchase_order_lines_successor')
        ORDER BY indexname""").all()
    assert len(indexes) == 2
    assert (
        "UNIQUE INDEX" in indexes[0].indexdef
        and "supersedes_line_id IS NULL" in indexes[0].indexdef
    )
    assert (
        "UNIQUE INDEX" in indexes[1].indexdef
        and "supersedes_line_id IS NOT NULL" in indexes[1].indexdef
    )
