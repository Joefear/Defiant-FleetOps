"""Direct app SQL and independent migrator DML must preserve immutable expectations."""

from decimal import Decimal
from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice8.conftest import line_values, order_values, snapshot, write
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import items, party_roles
from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders
from fleetops.domain.procurement import list_lines


@pytest.mark.parametrize("role", ["app", "migrator"])
@pytest.mark.parametrize(
    "table_name,field,value",
    [
        ("orders", "po_number", "changed"),
        ("orders", "vendor_party_id", None),
        ("orders", "notes", "changed"),
        ("orders", "status", "DRAFT"),
        ("orders", "issued_at", None),
        ("orders", "issued_by_actor_id", None),
        ("orders", "created_by_actor_id", None),
        ("orders", "updated_by_actor_id", None),
        ("orders", "created_at", None),
        ("orders", "updated_at", None),
        ("lines", "item_id", None),
        ("lines", "quantity", 99),
        ("lines", "uom", "M"),
        ("lines", "unit_price", 99),
        ("lines", "expected_date", "2030-01-01"),
        ("lines", "line_number", 2),
        ("lines", "po_id", None),
        ("lines", "supersedes_line_id", None),
        ("lines", "created_by_actor_id", None),
        ("lines", "updated_by_actor_id", None),
        ("lines", "created_at", None),
        ("lines", "updated_at", None),
        ("lines", "id", None),
        ("lines", "org_id", None),
        ("orders", "id", None),
        ("orders", "org_id", None),
    ],
)
def test_every_issued_field_is_frozen_even_for_ordinary_migrator(
    role,
    table_name,
    field,
    value,
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    table, row = (orders, issued[0]) if table_name == "orders" else (lines, issued[1])
    before = snapshot(migrator_connection, table, row["id"])
    connection = app_connection if role == "app" else migrator_connection
    with pytest.raises(DBAPIError) as error:
        with connection.begin():
            if role == "app":
                set_authenticated(connection, space_data[0])
            connection.execute(table.update().where(table.c.id == row["id"]).values({field: value}))
    assert error.value.orig.sqlstate in ({"23514", "42501"} if role == "app" else {"23514"})
    assert snapshot(migrator_connection, table, row["id"]) == before


@pytest.mark.parametrize("role", ["app", "migrator"])
@pytest.mark.parametrize("table", [orders, lines], ids=lambda t: t.name)
def test_issued_delete_cannot_reactivate_history(
    role,
    table,
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    row = issued[0 if table is orders else 1]
    connection = app_connection if role == "app" else migrator_connection
    with pytest.raises(DBAPIError) as error:
        with connection.begin():
            if role == "app":
                set_authenticated(connection, space_data[0])
            connection.execute(table.delete().where(table.c.id == row["id"]))
    assert error.value.orig.sqlstate == ("42501" if role == "app" else "23514")
    assert snapshot(migrator_connection, table, row["id"]) == dict(row)


def test_three_version_chain_preserves_every_predecessor_and_snapshots_each_item(
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    a, (po, original) = space_data[0], issued
    original_before = snapshot(migrator_connection, lines, original["id"])
    migrator_connection.execute(items.update().where(items.c.id == a.item_id).values(uom="M"))
    migrator_connection.commit()
    second = write(
        app_connection,
        a,
        lines.insert()
        .values(
            line_values(
                a,
                po["id"],
                id=UUID(int=2),
                supersedes_line_id=original["id"],
                unit_price="9.123456789123",
            )
        )
        .returning(lines),
    )
    assert original["uom"] == "EA" and second["uom"] == "M"
    assert snapshot(migrator_connection, lines, original["id"]) == original_before
    # A different Item is still the same business line, with its own catalog snapshot.
    other_item = uuid7()
    migrator_connection.execute(
        items.insert().values(
            id=other_item,
            org_id=a.org_id,
            manufacturer_party_id=a.party_id,
            manufacturer_part_number="REPLACEMENT",
            revision="A",
            description="Substitution",
            uom="KG",
            serialized=False,
            created_by_actor_id=a.actor_id,
            updated_by_actor_id=a.actor_id,
        )
    )
    migrator_connection.commit()
    third = write(
        app_connection,
        a,
        lines.insert()
        .values(
            line_values(
                a,
                po["id"],
                id=UUID(int=1),
                item_id=other_item,
                supersedes_line_id=second["id"],
                unit_price="1.50",
            )
        )
        .returning(lines),
    )
    assert third["uom"] == "KG" and third["line_number"] == second["line_number"] == 1
    assert snapshot(migrator_connection, lines, original["id"]) == original_before
    assert snapshot(migrator_connection, lines, second["id"]) == dict(second)
    assert second["unit_price"] == Decimal("9.123456789123")
    with app_connection.begin():
        set_authenticated(app_connection, a)
        history = list_lines(app_connection, po["id"])
    assert [r["id"] for r in history] == [original["id"], second["id"], third["id"]]
    assert [r["active"] for r in history] == [False, False, True]
    assert [r["superseded"] for r in history] == [True, True, False]
    # Successors freeze immediately; no re-open operation or predecessor write occurred.
    with pytest.raises(DBAPIError) as error:
        write(
            app_connection,
            a,
            lines.update().where(lines.c.id == third["id"]).values(unit_price=2).returning(lines),
        )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "attack",
    [
        "duplicate_root",
        "draft_successor",
        "wrong_number",
        "cross_po",
        "cross_org",
        "self",
        "branch",
        "new_issued_root",
    ],
)
def test_invalid_root_and_lineage_edges_reject_atomically(
    attack,
    draft,
    space_data,
    app_connection,
    migrator_connection,
):
    a, b = space_data
    po, line = draft
    values = line_values(a, po["id"])
    expected = "23514"
    if attack == "duplicate_root":
        expected = "23505"
    elif attack == "draft_successor":
        values["supersedes_line_id"] = line["id"]
    else:
        write(
            app_connection,
            a,
            orders.update()
            .where(orders.c.id == po["id"])
            .values(status="ISSUED")
            .returning(orders),
        )
        if attack != "new_issued_root":
            values["supersedes_line_id"] = line["id"]
            expected = "23503"
        if attack == "wrong_number":
            values["line_number"] = 2
        if attack in {"cross_po", "cross_org"}:
            tenant = a if attack == "cross_po" else b
            other = write(
                app_connection,
                tenant,
                orders.insert().values(order_values(tenant)).returning(orders),
            )
            write(
                app_connection,
                tenant,
                orders.update()
                .where(orders.c.id == other["id"])
                .values(status="ISSUED")
                .returning(orders),
            )
            values = line_values(tenant, other["id"], supersedes_line_id=line["id"])
            a = tenant
        if attack == "self":
            values["supersedes_line_id"] = values["id"]
        if attack == "branch":
            write(app_connection, a, lines.insert().values(values).returning(lines))
            values["id"] = uuid7()
            expected = "40001"
    before = migrator_connection.execute(select(lines)).all()
    migrator_connection.rollback()
    with pytest.raises(DBAPIError) as error:
        write(app_connection, a, lines.insert().values(values).returning(lines))
    assert error.value.orig.sqlstate == expected
    assert migrator_connection.execute(select(lines)).all() == before
    migrator_connection.rollback()


@pytest.mark.parametrize("count", [2, 3, 5])
def test_multirow_cycles_reject_even_with_set_constraints_all_deferred(
    count,
    issued,
    space_data,
    app_connection,
):
    a, po = space_data[0], issued[0]
    ids = [uuid7() for _ in range(count)]
    values = [
        line_values(a, po["id"], id=identity, supersedes_line_id=ids[(i + 1) % count])
        for i, identity in enumerate(ids)
    ]
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.exec_driver_sql("SET CONSTRAINTS ALL DEFERRED")
            app_connection.execute(lines.insert().values(values))
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("stage", ["draft", "issued", "successor"])
@pytest.mark.parametrize("pointer", ["null", "self", "other"])
def test_owner_cannot_attach_detach_redirect_or_rewrite_line_pointer(
    stage,
    pointer,
    draft,
    space_data,
    app_connection,
    migrator_connection,
):
    po, row = draft
    a = space_data[0]
    if stage != "draft":
        write(
            app_connection,
            a,
            orders.update()
            .where(orders.c.id == po["id"])
            .values(status="ISSUED")
            .returning(orders),
        )
    if stage == "successor":
        row = write(
            app_connection,
            a,
            lines.insert()
            .values(line_values(a, po["id"], supersedes_line_id=row["id"]))
            .returning(lines),
        )
    value = {"null": None, "self": row["id"], "other": uuid7()}[pointer]
    if stage == "draft" and pointer == "null":
        value = draft[1]["id"]  # NULL-to-reference must reject even before issuance.
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                lines.update().where(lines.c.id == row["id"]).values(supersedes_line_id=value)
            )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED"])
def test_future_status_does_not_grant_amendment_or_thaw_issued_history(
    status,
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    po, line = issued
    # Only fixture setup bypasses the unavailable transition; every assertion below
    # runs with the guard enabled. Slice 8 provides no close/cancel workflow.
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.purchase_orders DISABLE TRIGGER procurement_10_guard"
    )
    migrator_connection.execute(
        orders.update().where(orders.c.id == po["id"]).values(status=status)
    )
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.purchase_orders ENABLE TRIGGER procurement_10_guard"
    )
    migrator_connection.commit()
    with pytest.raises(DBAPIError) as error:
        write(
            app_connection,
            space_data[0],
            lines.insert()
            .values(line_values(space_data[0], po["id"], supersedes_line_id=line["id"]))
            .returning(lines),
        )
    assert error.value.orig.sqlstate == "23514"
    for statement in (
        lines.update().where(lines.c.id == line["id"]).values(quantity=3),
        lines.delete().where(lines.c.id == line["id"]),
        orders.update().where(orders.c.id == po["id"]).values(status="DRAFT"),
        orders.delete().where(orders.c.id == po["id"]),
    ):
        with pytest.raises(DBAPIError) as error:
            with migrator_connection.begin():
                migrator_connection.execute(statement)
        assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "0"),
        ("quantity", "-1"),
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("quantity", "-Infinity"),
        ("unit_price", "-0.01"),
        ("unit_price", "NaN"),
        ("unit_price", "Infinity"),
        ("line_number", 0),
        ("line_number", -1),
        ("expected_date", "infinity"),
    ],
)
def test_postgres_rejects_invalid_numeric_and_date_inputs(
    field, value, draft, space_data, app_connection
):
    with pytest.raises(DBAPIError) as error:
        write(
            app_connection,
            space_data[0],
            lines.insert()
            .values(line_values(space_data[0], draft[0]["id"], line_number=2) | {field: value})
            .returning(lines),
        )
    assert error.value.orig.sqlstate == "23514"


def test_decimal_storage_does_not_round_or_impose_integer_quantity(
    draft, space_data, app_connection
):
    quantity = "0.000000000000000000001234567890123456789"
    price = "12345678901234567890.12345678901234567890123456789"
    row = write(
        app_connection,
        space_data[0],
        lines.update()
        .where(lines.c.id == draft[1]["id"])
        .values(quantity=quantity, unit_price=price, expected_date=None)
        .returning(lines),
    )
    assert row["quantity"] == Decimal(quantity) and row["unit_price"] == Decimal(price)


@pytest.mark.parametrize("target", ["nonvendor", "foreign_vendor", "foreign_item", "foreign_po"])
def test_same_tenant_vendor_item_and_po_relationships(target, draft, space_data, app_connection):
    a, b = space_data
    if target in {"nonvendor", "foreign_vendor"}:
        statement = orders.insert().values(
            order_values(a, vendor_party_id=a.party_id if target == "nonvendor" else b.vendor_id)
        )
    else:
        values = line_values(a, draft[0]["id"], line_number=2)
        if target == "foreign_item":
            values["item_id"] = b.item_id
        else:
            other = write(
                app_connection, b, orders.insert().values(order_values(b)).returning(orders)
            )
            values["po_id"] = other["id"]
        statement = lines.insert().values(values)
    with pytest.raises(DBAPIError) as error:
        write(
            app_connection, a, statement.returning(orders if target.endswith("vendor") else lines)
        )
    assert error.value.orig.sqlstate == "23503"


def test_vendor_role_cannot_be_removed_while_referenced(draft, space_data, migrator_connection):
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                party_roles.delete().where(
                    party_roles.c.party_id == space_data[0].vendor_id,
                    party_roles.c.role == "VENDOR",
                )
            )
    assert error.value.orig.sqlstate == "23503"


def test_runnable_freeze_guards_are_invokers_with_no_runtime_ddl_authority(migrator_connection):
    rows = migrator_connection.execute(
        text("""
        SELECT proname, prosecdef, proconfig,
               has_function_privilege('fleetops_app', p.oid, 'EXECUTE')
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops' AND proname IN
          ('enforce_purchase_order', 'enforce_purchase_order_line') ORDER BY proname
    """)
    ).all()
    assert len(rows) == 2
    assert all(row[1:] == (False, ["search_path=pg_catalog, pg_temp"], False) for row in rows)
