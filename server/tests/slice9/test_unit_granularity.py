"""Serialized receipt-line granularity is independent of UOM (ADR-012 Decision 20)."""

from decimal import Decimal
from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    ASSET_TABLES,
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
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import receipt_lines

UOMS = ["EA", "M", "MM", "CM", "IN", "FT", "G", "MG", "KG", "ML", "L"]
NONUNIT_QUANTITIES = [Decimal("0.5"), Decimal(2), Decimal(5), Decimal(100)]


@pytest.mark.parametrize("uom", UOMS)
@pytest.mark.parametrize("quantity", NONUNIT_QUANTITIES, ids=str)
@pytest.mark.parametrize("known_conflict", [False, True], ids=["normal", "known_conflict"])
def test_serialized_nonunit_quantity_rejects_through_authenticated_api(
    uom, quantity, known_conflict, receiving_client, asset_data, migrator_connection
):
    a = asset_data[0]
    line = line_data(a, quantity=quantity, uom=uom)
    if known_conflict:
        line["unit"]["identifier"]["value"] = "SERIAL-001"
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post(
        "/receipts", headers=headers(a), json=json_data(receipt_data(a, lines=[line]))
    )
    assert response.status_code == 422, response.text
    # Every receiving row and every Asset/history/identifier column must survive unchanged.
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("uom", UOMS)
@pytest.mark.parametrize("quantity", NONUNIT_QUANTITIES, ids=str)
@pytest.mark.parametrize("known_conflict", [False, True], ids=["normal", "known_conflict"])
def test_serialized_nonunit_quantity_rejects_at_runtime_sql_boundary(
    uom, quantity, known_conflict, receiving_client, asset_data, app_connection, migrator_connection
):
    a = asset_data[0]
    receipt = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    before = snapshot(migrator_connection, a.org_id)
    # Normal admission uses its legitimate definer; known conflicts use ordinary INSERT.
    # Check the immediate granularity error, not a later missing-Exception failure.
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            if known_conflict:
                app_connection.execute(
                    receipt_lines.insert().values(
                        id=uuid7(),
                        org_id=a.org_id,
                        receipt_id=UUID(receipt["id"]),
                        item_id=a.item_id,
                        quantity=quantity,
                        uom=uom,
                        condition="GOOD",
                        owner_party_id=a.party_id,
                        observed_identifier_type="MANUFACTURER_SERIAL",
                        observed_identifier_value="SERIAL-001",
                    )
                )
            else:
                app_connection.execute(
                    text(UNIT_CALL),
                    unit_parameters(a, UUID(receipt["id"]), quantity=quantity, uom=uom),
                )
            pytest.fail("Serialized batch passed the durable INSERT boundary")
    assert error.value.orig.sqlstate == "23514"
    assert "represents one unit" in str(error.value.orig)
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("uom", UOMS)
@pytest.mark.parametrize("known_conflict", [False, True], ids=["normal", "known_conflict"])
def test_serialized_quantity_one_preserves_every_governed_uom_and_admission_contract(
    uom, known_conflict, receiving_client, asset_data, migrator_connection
):
    a = asset_data[0]
    line = line_data(a, quantity=1, uom=uom)
    if known_conflict:
        line["unit"]["identifier"]["value"] = "SERIAL-001"
    before = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
    result = post_receipt(receiving_client, a, receipt_data(a, lines=[line]))
    stored = result["lines"][0]
    assert stored["serialized"] and stored["quantity"] == "1" and stored["uom"] == uom
    after = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
    if known_conflict:
        assert kinds(result) == ["SERIAL_MISMATCH", "UNEXPECTED_ITEM"]
        assert stored["asset_id"] is None
        assert stored["observed_identifier_value"] == "SERIAL-001"
        mismatch = next(e for e in result["exceptions"] if e["exception_type"] == "SERIAL_MISMATCH")
        assert mismatch["conflicting_asset_id"] == str(a.asset_id)
        assert after == before
    else:
        assert kinds(result) == ["UNEXPECTED_ITEM"]
        assert stored["observed_identifier_type"] is stored["observed_identifier_value"] is None
        asset_id = UUID(stored["asset_id"])
        for table in ASSET_TABLES:
            added = [row for row in after[table.name] if row not in before[table.name]]
            if table.name in {
                "assets",
                "asset_identifiers",
                "asset_transitions",
                "asset_initial_facts",
                "asset_initial_assignment_facts",
            }:
                assert len(added) == 1
                assert added[0]._mapping["id" if table.name == "assets" else "asset_id"] == asset_id
            else:
                assert not added
        asset = next(row._mapping for row in after["assets"] if row._mapping["id"] == asset_id)
        assert asset["current_state"] == "RECEIVED" and asset["version"] == 1


@pytest.mark.parametrize("incremental", [False, True], ids=["full_delivery", "append"])
@pytest.mark.parametrize("known_conflict", [False, True], ids=["normal", "known_conflict"])
def test_failed_serialized_batch_preserves_the_applicable_atomic_boundary(
    incremental, known_conflict, receiving_client, asset_data, migrator_connection
):
    a = asset_data[0]
    accepted = line_data(a, condition="DAMAGED")
    receipt = None
    if incremental:
        receipt = post_receipt(
            receiving_client, a, receipt_data(a, lines=[accepted], reconcile=False)
        )
    invalid = line_data(a, quantity=100, uom="CM", condition="OPENED")
    if known_conflict:
        invalid["unit"]["identifier"]["value"] = "SERIAL-001"
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post(
        f"/receipts/{receipt['id']}/lines" if incremental else "/receipts",
        headers=headers(a),
        json=json_data(invalid if incremental else receipt_data(a, lines=[accepted, invalid])),
    )
    assert response.status_code == 422, response.text
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("expected,wanted", [(101, ["SHORT"]), (100, []), (99, ["OVER"])])
def test_nonserialized_hundred_cm_retains_receipt_local_quantity_arithmetic(
    expected, wanted, receiving_client, space_data, make_item, make_order, migrator_connection
):
    a = space_data[0]
    item = make_item(uom="CM")
    po, comparators = make_order(specs=[{"item_id": item, "quantity": expected}])
    before = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
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
                    po_line_id=comparators[0]["id"],
                    quantity=100,
                    uom="CM",
                ),
            ],
        ),
    )
    assert kinds(result) == wanted
    stored = result["lines"][0]
    assert not stored["serialized"]
    assert stored["quantity"] == "100" and stored["uom"] == "CM" and stored["asset_id"] is None
    assert snapshot(migrator_connection, a.org_id, ASSET_TABLES) == before
