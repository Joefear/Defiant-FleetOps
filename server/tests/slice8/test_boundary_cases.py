"""Draft snapshots, server-owned attribution, and pointer order at adversarial boundaries."""

from datetime import UTC, datetime

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice8.conftest import headers, line_values, snapshot, write
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import items
from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders
from fleetops.db.tenancy import set_organization
from fleetops.domain.procurement import list_lines


def test_draft_item_edit_preserves_snapshot_and_new_root_captures_current_default(
    draft,
    space_data,
    app_connection,
    migrator_connection,
):
    a, (po, root) = space_data[0], draft
    item_id = uuid7()
    migrator_connection.execute(
        items.insert().values(
            id=item_id,
            org_id=a.org_id,
            manufacturer_party_id=a.party_id,
            manufacturer_part_number="DRAFT-ITEM",
            revision="A",
            description="Draft replacement",
            uom="M",
            serialized=False,
            created_by_actor_id=a.actor_id,
            updated_by_actor_id=a.actor_id,
        )
    )
    migrator_connection.commit()
    changed = write(
        app_connection,
        a,
        lines.update().where(lines.c.id == root["id"]).values(item_id=item_id).returning(lines),
    )
    assert changed["item_id"] == item_id and changed["uom"] == root["uom"] == "EA"
    second = write(
        app_connection,
        a,
        lines.insert()
        .values(line_values(a, po["id"], item_id=item_id, line_number=2))
        .returning(lines),
    )
    assert second["uom"] == "M"
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                lines.update().where(lines.c.id == root["id"]).values(uom="M")
            )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("times", ["equal", "reversed"])
def test_history_follows_pointers_with_equal_or_reversed_record_times(
    times,
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    a, (po, first) = space_data[0], issued
    second = write(
        app_connection,
        a,
        lines.insert()
        .values(line_values(a, po["id"], supersedes_line_id=first["id"]))
        .returning(lines),
    )
    third = write(
        app_connection,
        a,
        lines.insert()
        .values(line_values(a, po["id"], supersedes_line_id=second["id"]))
        .returning(lines),
    )
    ids = [first["id"], second["id"], third["id"]]
    # Deliberately adversarial clock fixtures isolate read semantics, not write
    # authority. Ordinary owner UPDATE rejection is proved with active guards in
    # test_invariants. No production operation can write these timestamp claims.
    with migrator_connection.begin():
        migrator_connection.exec_driver_sql(
            "ALTER TABLE fleetops.purchase_order_lines DISABLE TRIGGER procurement_10_guard"
        )
        for i, identity in enumerate(ids):
            instant = datetime(2030 if times == "equal" else 2030 - i, 1, 1, tzinfo=UTC)
            migrator_connection.execute(
                lines.update()
                .where(lines.c.id == identity)
                .values(created_at=instant, updated_at=instant)
            )
        migrator_connection.exec_driver_sql(
            "ALTER TABLE fleetops.purchase_order_lines ENABLE TRIGGER procurement_10_guard"
        )
    before = [snapshot(migrator_connection, lines, identity) for identity in ids]
    with app_connection.begin():
        set_authenticated(app_connection, a)
        history = list_lines(app_connection, po["id"])
    assert [row["id"] for row in history] == ids
    assert [row["active"] for row in history] == [False, False, True]
    assert [
        {k: v for k, v in row.items() if k not in {"active", "superseded"}} for row in history
    ] == before


def test_direct_issue_derives_issuer_despite_false_updater_testimony(
    draft,
    space_data,
    other_human,
    app_connection,
):
    po = write(
        app_connection,
        other_human,
        orders.update()
        .where(orders.c.id == draft[0]["id"])
        .values(
            status="ISSUED",
            updated_by_actor_id=space_data[0].actor_id,
            updated_at="1900-01-01T00:00:00Z",
        )
        .returning(orders),
    )
    assert po["issued_by_actor_id"] == po["updated_by_actor_id"] == other_human.actor_id
    assert po["created_by_actor_id"] == space_data[0].actor_id
    assert po["issued_at"] == po["updated_at"] > draft[0]["updated_at"]


@pytest.mark.parametrize("operation", ["edit_po", "edit_line", "issue", "successor"])
def test_organization_only_context_cannot_perform_attributed_procurement_writes(
    operation,
    draft,
    space_data,
    app_connection,
):
    po, root = draft
    a = space_data[0]
    if operation == "successor":
        write(
            app_connection,
            a,
            orders.update()
            .where(orders.c.id == po["id"])
            .values(status="ISSUED")
            .returning(orders),
        )
    statement = {
        "edit_po": orders.update().where(orders.c.id == po["id"]).values(notes="spoof"),
        "edit_line": lines.update().where(lines.c.id == root["id"]).values(quantity=7),
        "issue": orders.update().where(orders.c.id == po["id"]).values(status="ISSUED"),
        "successor": lines.insert().values(line_values(a, po["id"], supersedes_line_id=root["id"])),
    }[operation]
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("status", ["CLOSED", "CANCELLED", "invalid"])
def test_no_generic_status_transition_is_inferred(status, draft, space_data, app_connection):
    with pytest.raises(DBAPIError) as error:
        write(
            app_connection,
            space_data[0],
            orders.update()
            .where(orders.c.id == draft[0]["id"])
            .values(status=status)
            .returning(orders),
        )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "table,field",
    [
        (orders, "created_at"),
        (orders, "updated_at"),
        (orders, "issued_at"),
        (lines, "created_at"),
        (lines, "updated_at"),
        (lines, "uom"),
    ],
)
def test_runtime_cannot_supply_server_owned_insert_columns(
    table,
    field,
    draft,
    space_data,
    app_connection,
):
    from server.tests.slice8.conftest import order_values

    a = space_data[0]
    values = order_values(a) if table is orders else line_values(a, draft[0]["id"], line_number=2)
    values[field] = "M" if field == "uom" else "1900-01-01T00:00:00Z"
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(table.insert().values(values))
    assert error.value.orig.sqlstate == "42501"


def test_supersession_failure_rolls_back_preceding_transaction_edits(
    issued,
    space_data,
    app_connection,
    migrator_connection,
):
    a, (po, root) = space_data[0], issued
    item = snapshot(migrator_connection, items, a.item_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(items.update().where(items.c.id == a.item_id).values(uom="M"))
            app_connection.execute(
                lines.insert().values(
                    line_values(a, po["id"], supersedes_line_id=root["id"], quantity=-1)
                )
            )
    assert error.value.orig.sqlstate == "23514"
    assert snapshot(migrator_connection, items, a.item_id) == item
    assert snapshot(migrator_connection, lines, root["id"]) == dict(root)
    assert migrator_connection.execute(
        select(lines.c.id).where(lines.c.po_id == po["id"])
    ).all() == [(root["id"],)]


def test_unamended_multiple_roots_are_all_active_and_line_route_requires_auth(
    draft,
    space_data,
    space_client,
    app_connection,
):
    a, (po, root) = space_data[0], draft
    second = write(
        app_connection,
        a,
        lines.insert().values(line_values(a, po["id"], line_number=2)).returning(lines),
    )
    path = f"/purchase-orders/{po['id']}/lines"
    assert space_client.post(path, json={}).status_code == 401
    rows = space_client.get(path, headers=headers(a)).json()
    assert [r["id"] for r in rows] == [str(root["id"]), str(second["id"])]
    assert all(r["active"] and not r["superseded"] for r in rows)
