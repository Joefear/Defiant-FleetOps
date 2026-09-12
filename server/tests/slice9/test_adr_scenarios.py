"""Accepted ADR scenarios spanning comparator admission, physical history, and SQL-only attacks."""

from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    RECEIVING_TABLES,
    UNIT_CALL,
    headers,
    json_data,
    kinds,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
    unit_parameters,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from fleetops.db.metadata import (
    asset_identifiers,
    asset_initial_assignment_facts,
    asset_initial_facts,
    asset_movements,
    asset_transitions,
    assets,
    items,
    purchase_order_lines,
    purchase_orders,
    receipt_lines,
    receipts,
)
from fleetops.db.tenancy import set_organization
from fleetops.domain import procurement


@pytest.mark.parametrize("actual,wanted", [(1, ["SHORT"]), (3, ["OVER"]), (2, [])])
def test_exact_adr_two_ea_examples(
    actual, wanted, receiving_client, space_data, make_item, make_order
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 2}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=actual)
            ],
        ),
    )
    assert kinds(result) == wanted


def test_same_receipt_four_plus_five_is_nine_not_current_payload_five(
    receiving_client, space_data, make_item, make_order
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 10}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=q)
                for q in [4, 5]
            ],
        ),
    )
    assert kinds(result) == ["SHORT"]
    assert len(result["lines"]) == 2


@pytest.mark.parametrize("expected_uom,actual_uom,quantity", [("M", "CM", 100), ("KG", "G", 1000)])
def test_dimensionally_related_tokens_are_still_incomparable(
    expected_uom, actual_uom, quantity, receiving_client, space_data, make_item, make_order
):
    a = space_data[0]
    item = make_item(uom=expected_uom)
    po, expected = make_order(specs=[{"item_id": item, "quantity": 1}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    quantity=quantity,
                    uom=actual_uom,
                )
            ],
        ),
    )
    assert kinds(result) == ["UOM_MISMATCH"]
    assert result["lines"][0]["quantity"] == str(quantity)


@pytest.mark.parametrize("failure", ["draft", "same_org_other_po", "stale", "reused_stale_binding"])
def test_comparator_authority_rejects_invalid_targets_without_retargeting(
    failure, receiving_client, space_data, make_order, app_connection, migrator_connection
):
    a = space_data[0]
    po, expected = make_order(issue=failure != "draft")
    target = expected[0]["id"]
    receipt = None
    if failure == "same_org_other_po":
        _, other = make_order()
        target = other[0]["id"]
    if failure == "reused_stale_binding":
        receipt = post_receipt(
            receiving_client,
            a,
            receipt_data(a, po_id=po["id"], comparator_ids=[target], reconcile=False),
        )
    if failure in {"stale", "reused_stale_binding"}:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            procurement.create_line(
                app_connection,
                po["id"],
                predecessor_id=target,
                performer_id=a.actor_id,
                values={
                    "item_id": a.item_id,
                    "quantity": 1,
                    "unit_price": 7,
                    "expected_date": None,
                },
            )
    before = snapshot(migrator_connection, a.org_id)
    line = line_data(a, po_line_id=target)
    response = receiving_client.post(
        f"/receipts/{receipt['id']}/lines" if receipt else "/receipts",
        headers=headers(a),
        json=json_data(line if receipt else receipt_data(a, po_id=po["id"], lines=[line])),
    )
    assert response.status_code == (409 if "stale" in failure else 422), response.text
    assert snapshot(migrator_connection, a.org_id) == before


def test_successor_and_predecessor_never_share_quantity_or_recompute_earlier_mismatch(
    receiving_client, space_data, make_item, make_order, app_connection, migrator_connection
):
    a = space_data[0]
    item = make_item(uom="M")
    po, expected = make_order(specs=[{"item_id": item, "quantity": 10}])
    r1 = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=300, uom="CM"
                )
            ],
        ),
    )
    assert kinds(r1) == ["UOM_MISMATCH"]
    # A later receipt on the same version remains independently SHORT.
    r2 = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=8, uom="M"
                )
            ],
        ),
    )
    assert kinds(r2) == ["SHORT"]
    with app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.execute(items.update().where(items.c.id == item).values(uom="EA"))
        successor = procurement.create_line(
            app_connection,
            po["id"],
            predecessor_id=expected[0]["id"],
            performer_id=a.actor_id,
            values={"item_id": item, "quantity": 10, "unit_price": 1, "expected_date": None},
        )
    before = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
    r3 = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[line_data(a, item_id=item, unit=None, po_line_id=successor["id"], quantity=4)],
        ),
    )
    assert kinds(r3) == ["SHORT"]
    assert receiving_client.get(f"/receipts/{r1['id']}", headers=headers(a)).json() == r1
    assert receiving_client.get(f"/receipts/{r2['id']}", headers=headers(a)).json() == r2
    assert (
        snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines)) == before
    )


def test_normal_unit_with_uom_substitution_condition_packing_and_unreadable_still_has_full_bundle(
    receiving_client, space_data, make_item, make_order, migrator_connection
):
    a = space_data[0]
    actual = make_item(serialized=True)
    po, expected = make_order()
    # One physical unit keeps all five independently supported disagreements.
    line = line_data(
        a,
        item_id=actual,
        po_line_id=expected[0]["id"],
        quantity=1,
        uom="CM",
        condition="OPENED",
        packing_quantity=2,
    )
    line["unit"]["identifier"] = {
        "type": "OTHER",
        "value": None,
        "unreadable_reason": "Unreadable mark",
    }
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(a, po_id=po["id"], packing_reference="Packing lists two units", lines=[line]),
    )
    assert kinds(result) == [
        "OPENED",
        "QUANTITY_VARIANCE",
        "SERIAL_UNREADABLE",
        "SUBSTITUTION",
        "UOM_MISMATCH",
    ]
    stored = result["lines"][0]
    assert stored["quantity"] == "1" and stored["uom"] == "CM" and stored["serialized"]
    asset_id = UUID(stored["asset_id"])
    for table in [
        assets,
        asset_transitions,
        asset_initial_facts,
        asset_initial_assignment_facts,
        asset_identifiers,
    ]:
        identity = table.c.id if table is assets else table.c.asset_id
        assert (
            len(migrator_connection.execute(select(table).where(identity == asset_id)).all()) == 1
        )
    for exception in result["exceptions"]:
        assert exception["asset_id"] == (
            None if exception["exception_type"] == "QUANTITY_VARIANCE" else str(asset_id)
        )


def test_later_movement_starts_at_receipt_dock_and_preserves_independent_initial_facts(
    receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    result = post_receipt(receiving_client, a, receipt_data(a, lines=[line_data(a)]))
    asset_id = UUID(result["lines"][0]["asset_id"])
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post(
        f"/assets/{asset_id}/movements",
        headers=headers(a),
        json={
            "expected_version": 1,
            "to_location_id": str(a.other_location_id),
            "reason": "Scanned onto storage shelf",
            "occurred_at": "2026-09-12T10:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    movement = (
        migrator_connection.execute(
            select(asset_movements).where(asset_movements.c.asset_id == asset_id)
        )
        .mappings()
        .one()
    )
    assert movement["from_location_id"] == a.location_id
    assert movement["to_location_id"] == a.other_location_id
    assert movement["result_version"] == 2
    after = snapshot(migrator_connection, a.org_id)
    for table in (*RECEIVING_TABLES, *ASSET_TABLES):
        if table.name not in {"assets", "asset_movements"}:
            assert after[table.name] == before[table.name]
    asset = (
        migrator_connection.execute(select(assets).where(assets.c.id == asset_id)).mappings().one()
    )
    assert asset["version"] == 2 and asset["current_state"] == "RECEIVED"
    assert asset["owner_party_id"] == a.party_id and asset["custodian_party_id"] is None
    assert asset["current_assignment_id"] is None
    assert asset["current_location_id"] == a.other_location_id


@pytest.mark.parametrize(
    "field", ["receipt_id", "po_line_id", "item_id", "owner_party_id", "custodian_party_id"]
)
def test_definer_validates_each_foreign_business_target_explicitly(
    field, receiving_client, space_data, make_order, app_connection, migrator_connection
):
    a, b = space_data
    po, expected = make_order(tenant=b)
    own = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    foreign = post_receipt(receiving_client, b, receipt_data(b, po_id=po["id"], reconcile=False))
    values = unit_parameters(a, UUID(own["id"]))
    values[field] = {
        "receipt_id": UUID(foreign["id"]),
        "po_line_id": expected[0]["id"],
        "item_id": b.item_id,
        "owner_party_id": b.party_id,
        "custodian_party_id": b.party_id,
    }[field]
    before = snapshot(migrator_connection, a.org_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(text(UNIT_CALL), values)
    assert error.value.orig.sqlstate in {"23503", "23514"}
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("scope", ["missing", "invalid", "cross"])
@pytest.mark.parametrize("table", RECEIVING_TABLES, ids=lambda t: t.name)
def test_runtime_missing_invalid_cross_scope_cannot_write_any_receiving_table(
    scope, table, receiving_client, space_data, make_item, app_connection, migrator_connection
):
    a, b = space_data
    result = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    before = snapshot(migrator_connection, a.org_id)
    # All statements use granted business columns; failure is the real credential/scope boundary.
    values = {"id": UUID(result["id"]), "org_id": a.org_id}
    if table is receipts:
        values |= {"vendor_party_id": a.vendor_id, "received_at": receipt_data(a)["received_at"]}
    else:
        values["receipt_id"] = UUID(result["id"])
    if table is receipt_lines:
        values |= {"item_id": make_item(), "quantity": 1, "uom": "EA", "condition": "GOOD"}
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if scope == "cross":
                set_authenticated(app_connection, b)
            elif scope == "invalid":
                app_connection.execute(text("SELECT set_config('fleetops.org_id','invalid',true)"))
            app_connection.execute(table.insert().values(values))
    assert error.value.orig.sqlstate in {"42501", "23503", "22P02"}
    assert snapshot(migrator_connection, a.org_id) == before


def test_foreign_receipt_hidden_across_all_read_and_mutation_routes(receiving_client, space_data):
    a, b = space_data
    result = post_receipt(receiving_client, b, receipt_data(b, reconcile=False))
    assert receiving_client.get("/receipts", headers=headers(a)).json() == []
    for method, suffix, body in [
        ("GET", "", None),
        ("POST", "/reconcile", {}),
        ("POST", "/lines", json_data(line_data(a))),
    ]:
        response = receiving_client.request(
            method, f"/receipts/{result['id']}{suffix}", headers=headers(a), json=body
        )
        assert response.status_code == 404


@pytest.mark.parametrize("condition", ["", "DAMAGED,OPENED", "bad", "UNREADABLE_IDENTIFIER"])
def test_sql_rejects_ungoverned_condition_values(
    condition, receiving_client, space_data, make_item, app_connection
):
    a = space_data[0]
    result = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    item = make_item()
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                receipt_lines.insert().values(
                    id=UUID(result["id"]),
                    org_id=a.org_id,
                    receipt_id=UUID(result["id"]),
                    item_id=item,
                    quantity=1,
                    uom="EA",
                    condition=condition,
                )
            )
    assert error.value.orig.sqlstate == "23514"


def test_org_only_read_scope_cannot_invoke_normal_asset_creation(
    receiving_client, space_data, app_connection
):
    a = space_data[0]
    result = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(text(UNIT_CALL), unit_parameters(a, UUID(result["id"])))
    assert error.value.orig.sqlstate == "42501"
