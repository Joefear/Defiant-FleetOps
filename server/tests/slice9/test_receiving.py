"""Receiving behavior proved through the production API and independent stored-row queries."""

from collections import Counter
from decimal import Decimal
from uuid import UUID

import pytest
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    WHEN,
    headers,
    json_data,
    kinds,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)
from sqlalchemy import select

from fleetops.db.metadata import (
    asset_identifiers,
    asset_initial_assignment_facts,
    asset_initial_facts,
    asset_transitions,
    assets,
    purchase_order_lines,
    purchase_orders,
    receipt_lines,
)


def test_normal_unit_commits_complete_initial_bundle_and_explicit_business_facts(
    receiving_client,
    space_data,
    make_order,
    migrator_connection,
):
    a = space_data[0]
    po, expected = make_order()
    before = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
    captured = line_data(a, po_line_id=expected[0]["id"])
    received = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[captured],
        ),
    )
    assert received["reconciled"] and received["exceptions"] == []
    assert len(received["lines"]) == 1
    line = received["lines"][0]
    asset_id = UUID(line["asset_id"])
    asset = (
        migrator_connection.execute(select(assets).where(assets.c.id == asset_id)).mappings().one()
    )
    baseline = (
        migrator_connection.execute(
            select(asset_initial_facts).where(asset_initial_facts.c.asset_id == asset_id)
        )
        .mappings()
        .one()
    )
    transition = (
        migrator_connection.execute(
            select(asset_transitions).where(asset_transitions.c.asset_id == asset_id)
        )
        .mappings()
        .one()
    )
    witness = (
        migrator_connection.execute(
            select(asset_initial_assignment_facts).where(
                asset_initial_assignment_facts.c.asset_id == asset_id
            )
        )
        .mappings()
        .one()
    )
    identifier = (
        migrator_connection.execute(
            select(asset_identifiers).where(asset_identifiers.c.asset_id == asset_id)
        )
        .mappings()
        .one()
    )
    assert asset["version"] == transition["result_version"] == 1
    assert asset["current_state"] == transition["to_state"] == "RECEIVED"
    assert transition["from_state"] is None and asset["current_assignment_id"] is None
    assert asset["owner_party_id"] == baseline["initial_owner_party_id"] == a.party_id
    assert a.party_id != a.actor_id and a.party_id != a.vendor_id
    assert asset["custodian_party_id"] is baseline["initial_custodian_party_id"] is None
    assert asset["current_location_id"] == baseline["initial_location_id"] == a.location_id
    assert asset["created_by_actor_id"] == a.actor_id
    assert baseline["actor_id"] == transition["actor_id"] == witness["actor_id"] == a.actor_id
    assert baseline["occurred_at"] == transition["occurred_at"] == witness["occurred_at"] == WHEN
    assert identifier["value"] == captured["unit"]["identifier"]["value"]
    assert identifier["unreadable_reason"] is None
    assert line["observed_identifier_type"] is line["observed_identifier_value"] is None
    migrator_connection.rollback()
    assert (
        snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines)) == before
    )


@pytest.mark.parametrize("quantity,expected_kind", [("8", "SHORT"), ("12", "OVER"), ("10", None)])
def test_receipt_local_quantity_outcomes_have_aggregate_shapes(
    quantity,
    expected_kind,
    receiving_client,
    space_data,
    make_item,
    make_order,
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
                line_data(
                    a,
                    item_id=item,
                    quantity=quantity,
                    unit=None,
                    po_line_id=expected[0]["id"],
                )
            ],
        ),
    )
    assert kinds(result) == ([] if expected_kind is None else [expected_kind])
    for error in result["exceptions"]:
        assert error["receipt_id"] == result["id"] and error["po_line_id"] == str(expected[0]["id"])
        assert (
            error["receipt_line_id"] is error["asset_id"] is error["conflicting_asset_id"] is None
        )


def test_known_conflict_records_observation_without_asset_and_preserves_existing_unit(
    receiving_client,
    asset_data,
    make_order,
    migrator_connection,
):
    a = asset_data[0]
    po, expected = make_order()
    before = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
    line = line_data(a, po_line_id=expected[0]["id"], condition="DAMAGED")
    line["unit"]["identifier"]["value"] = "SERIAL-001"
    result = post_receipt(receiving_client, a, receipt_data(a, po_id=po["id"], lines=[line]))
    assert kinds(result) == ["DAMAGED", "SERIAL_MISMATCH"]
    observed = result["lines"][0]
    assert observed["asset_id"] is None
    assert observed["observed_identifier_type"] == "MANUFACTURER_SERIAL"
    assert observed["observed_identifier_value"] == "SERIAL-001"
    mismatch = next(e for e in result["exceptions"] if e["exception_type"] == "SERIAL_MISMATCH")
    assert mismatch["conflicting_asset_id"] == str(a.asset_id)
    assert mismatch["asset_id"] is None and mismatch["receipt_line_id"] == observed["id"]
    damaged = next(e for e in result["exceptions"] if e["exception_type"] == "DAMAGED")
    assert damaged["asset_id"] is damaged["conflicting_asset_id"] is None
    assert snapshot(migrator_connection, a.org_id, ASSET_TABLES) == before


def test_line_by_line_capture_reconciles_full_receipt_without_transient_quantity_errors(
    receiving_client,
    space_data,
    make_item,
    make_order,
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
            comparator_ids=[expected[0]["id"]],
            reconcile=False,
        ),
    )
    for quantity in ("4", "6"):
        response = receiving_client.post(
            f"/receipts/{result['id']}/lines",
            headers=headers(a),
            json=json_data(
                line_data(
                    a,
                    item_id=item,
                    quantity=quantity,
                    unit=None,
                    po_line_id=expected[0]["id"],
                )
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["exceptions"] == []
    response = receiving_client.post(f"/receipts/{result['id']}/reconcile", headers=headers(a))
    assert response.status_code == 200, response.text
    completed = response.json()
    assert completed["reconciled"] and completed["exceptions"] == []
    assert sum(Decimal(line["quantity"]) for line in completed["lines"]) == 10
    assert (
        receiving_client.post(f"/receipts/{result['id']}/reconcile", headers=headers(a)).status_code
        == 409
    )


@pytest.mark.parametrize("incremental", [False, True])
def test_frozen_delivery_creates_exact_four_exceptions_nine_assets_and_unchanged_po(
    incremental,
    receiving_client,
    space_data,
    make_item,
    make_order,
    migrator_connection,
):
    a = space_data[0]
    workstation = a.item_id
    substitute, scanner, printer, network = [make_item(serialized=True) for _ in range(4)]
    accessory = make_item()
    po, expected = make_order(
        specs=[
            {"item_id": workstation, "quantity": 6},
            {"item_id": scanner, "quantity": 2},
            {"item_id": printer, "quantity": 1},
            {"item_id": network, "quantity": 1},
        ]
    )
    before = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
    delivery = [line_data(a, po_line_id=expected[0]["id"]) for _ in range(5)]
    delivery[0]["unit"]["identifier"] = {
        "type": "MANUFACTURER_SERIAL",
        "value": None,
        "unreadable_reason": "Label abraded",
    }
    delivery.append(line_data(a, item_id=substitute, po_line_id=expected[0]["id"]))
    for item, comparator in zip((scanner, printer, network), expected[1:], strict=True):
        delivery.append(line_data(a, item_id=item, po_line_id=comparator["id"]))
    delivery.append(line_data(a, item_id=accessory, unit=None))
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            comparator_ids=[line["id"] for line in expected],
            lines=[] if incremental else delivery,
            reconcile=not incremental,
        ),
    )
    if incremental:
        for line in delivery:
            response = receiving_client.post(
                f"/receipts/{result['id']}/lines",
                headers=headers(a),
                json=json_data(line),
            )
            assert response.status_code == 201, response.text
        response = receiving_client.post(
            f"/receipts/{result['id']}/reconcile",
            headers=headers(a),
        )
        assert response.status_code == 200, response.text
        result = response.json()
    assert Counter(kinds(result)) == Counter(
        {
            "SUBSTITUTION": 1,
            "SERIAL_UNREADABLE": 1,
            "SHORT": 1,
            "UNEXPECTED_ITEM": 1,
        }
    )
    actual_assets = (
        migrator_connection.execute(select(assets).where(assets.c.org_id == a.org_id))
        .mappings()
        .all()
    )
    assert len(actual_assets) == 9
    assert all(row["current_state"] == "RECEIVED" and row["version"] == 1 for row in actual_assets)
    assert Counter(row["item_id"] for row in actual_assets) == Counter(
        {
            workstation: 5,
            substitute: 1,
            scanner: 1,
            printer: 1,
            network: 1,
        }
    )
    accessory_line = next(line for line in result["lines"] if line["item_id"] == str(accessory))
    assert accessory_line["asset_id"] is None and accessory_line["po_line_id"] is None
    errors = {row["exception_type"]: row for row in result["exceptions"]}
    assert errors["SHORT"]["po_line_id"] == str(expected[1]["id"])
    assert errors["SHORT"]["receipt_line_id"] is None
    assert errors["UNEXPECTED_ITEM"]["receipt_line_id"] == accessory_line["id"]
    unreadable = errors["SERIAL_UNREADABLE"]
    assert unreadable["asset_id"] is not None and unreadable["po_line_id"] == str(expected[0]["id"])
    for table in (
        asset_initial_facts,
        asset_initial_assignment_facts,
        asset_transitions,
        asset_identifiers,
    ):
        assert (
            len(migrator_connection.execute(select(table).where(table.c.org_id == a.org_id)).all())
            == 9
        )
    # Cost remains a provenance join, including the substituted actual Item.
    prices = (
        migrator_connection.execute(
            select(purchase_order_lines.c.unit_price)
            .join(receipt_lines, receipt_lines.c.po_line_id == purchase_order_lines.c.id)
            .where(receipt_lines.c.asset_id.is_not(None), receipt_lines.c.org_id == a.org_id)
        )
        .scalars()
        .all()
    )
    assert prices == [Decimal("123.123456789")] * 9
    migrator_connection.rollback()
    assert (
        snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines)) == before
    )
