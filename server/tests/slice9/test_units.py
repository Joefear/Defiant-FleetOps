"""Unit admission, independent anomaly composition, and request authority on PostgreSQL 16."""

from copy import deepcopy
from uuid import UUID

import pytest
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    headers,
    json_data,
    kinds,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)
from sqlalchemy import select

from fleetops.db.metadata import asset_identifiers, asset_initial_facts, assets

CONDITIONS = ["GOOD", "DAMAGED", "OPENED", "UNKNOWN"]
IDENTIFIERS = ["MANUFACTURER_SERIAL", "PCB_SERIAL", "MAC", "IMEI", "OTHER"]


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("backed", [False, True])
@pytest.mark.parametrize("serialized", [False, True])
def test_single_condition_and_conditional_comparator(
    condition, backed, serialized, receiving_client, space_data, make_item, make_order
):
    a = space_data[0]
    item = make_item(serialized=serialized)
    po, expected = make_order(specs=[{"item_id": item, "quantity": 1}])
    line = line_data(a, item_id=item, condition=condition)
    if not serialized:
        line["unit"] = None
    if backed:
        line["po_line_id"] = expected[0]["id"]
    result = post_receipt(
        receiving_client, a, receipt_data(a, po_id=po["id"] if backed else None, lines=[line])
    )
    wanted = ([] if backed else ["UNEXPECTED_ITEM"]) + (
        [condition] if condition in {"DAMAGED", "OPENED"} else []
    )
    assert kinds(result) == sorted(wanted)
    assert result["lines"][0]["condition"] == condition
    assert bool(result["lines"][0]["asset_id"]) is serialized
    for exception in result["exceptions"]:
        assert exception["po_line_id"] == (str(expected[0]["id"]) if backed else None)
        assert exception["asset_id"] == result["lines"][0]["asset_id"]
        assert exception["conflicting_asset_id"] is None


@pytest.mark.parametrize("condition", CONDITIONS)
@pytest.mark.parametrize("different_uom", [False, True])
@pytest.mark.parametrize("substitution", [False, True])
def test_known_conflict_composes_independent_facts_without_touching_any_asset_fact(
    condition,
    different_uom,
    substitution,
    receiving_client,
    asset_data,
    make_item,
    make_order,
    migrator_connection,
):
    a = asset_data[0]
    actual = make_item(serialized=True) if substitution else a.item_id
    po, expected = make_order()
    before = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
    line = line_data(
        a,
        item_id=actual,
        po_line_id=expected[0]["id"],
        condition=condition,
        uom="CM" if different_uom else "EA",
        packing_quantity=2,
    )
    line["unit"]["identifier"]["value"] = "SERIAL-001"
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(a, po_id=po["id"], packing_reference="Packing 2", lines=[line]),
    )
    wanted = ["SERIAL_MISMATCH", "QUANTITY_VARIANCE"]
    if condition in {"DAMAGED", "OPENED"}:
        wanted.append(condition)
    if different_uom:
        wanted.append("UOM_MISMATCH")
    if substitution:
        wanted.append("SUBSTITUTION")
    assert kinds(result) == sorted(wanted)
    stored = result["lines"][0]
    assert stored["asset_id"] is None
    assert stored["item_id"] == str(actual)
    assert stored["observed_identifier_type"] == "MANUFACTURER_SERIAL"
    assert stored["observed_identifier_value"] == "SERIAL-001"
    assert stored["quantity"] == "1" and stored["uom"] == line["uom"]
    for exception in result["exceptions"]:
        assert exception["asset_id"] is None
        assert exception["conflicting_asset_id"] == (
            str(a.asset_id) if exception["exception_type"] == "SERIAL_MISMATCH" else None
        )
    assert snapshot(migrator_connection, a.org_id, ASSET_TABLES) == before


def test_repeated_known_observations_are_nonunique_and_count_each_physical_unit(
    receiving_client, asset_data, make_order, migrator_connection
):
    a = asset_data[0]
    po, expected = make_order(specs=[{"item_id": a.item_id, "quantity": 2}])
    before = snapshot(migrator_connection, a.org_id, ASSET_TABLES)
    lines = [line_data(a, po_line_id=expected[0]["id"]) for _ in range(2)]
    for line in lines:
        line["unit"]["identifier"]["value"] = "SERIAL-001"
    result = post_receipt(receiving_client, a, receipt_data(a, po_id=po["id"], lines=lines))
    assert kinds(result) == ["SERIAL_MISMATCH", "SERIAL_MISMATCH"]
    assert len({line["id"] for line in result["lines"]}) == 2
    assert all(line["asset_id"] is None for line in result["lines"])
    assert snapshot(migrator_connection, a.org_id, ASSET_TABLES) == before


@pytest.mark.parametrize("kind", IDENTIFIERS)
@pytest.mark.parametrize("unreadable", [False, True])
def test_identifier_vocabulary_preserves_readable_values_or_explicit_null_reason(
    kind, unreadable, receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    line = line_data(a)
    raw = "  serial case / A:123  "
    line["unit"]["identifier"] = {
        "type": kind,
        "value": None if unreadable else raw,
        "unreadable_reason": "Etched serial obscured" if unreadable else None,
    }
    result = post_receipt(receiving_client, a, receipt_data(a, lines=[line]))
    stored = result["lines"][0]
    assert stored["observed_identifier_type"] is stored["observed_identifier_value"] is None
    canonical = (
        migrator_connection.execute(
            select(asset_identifiers).where(
                asset_identifiers.c.asset_id == UUID(stored["asset_id"])
            )
        )
        .mappings()
        .one()
    )
    assert canonical["type"] == kind
    assert canonical["value"] == (None if unreadable else raw)
    assert canonical["unreadable_reason"] == ("Etched serial obscured" if unreadable else None)
    assert kinds(result) == (
        ["SERIAL_UNREADABLE", "UNEXPECTED_ITEM"] if unreadable else ["UNEXPECTED_ITEM"]
    )


def test_other_identifier_type_and_other_tenant_value_are_not_local_conflicts(
    receiving_client, asset_data, migrator_connection
):
    a, b = asset_data
    line = line_data(a)
    line["unit"]["identifier"] = {"type": "PCB_SERIAL", "value": "SERIAL-001"}
    first = post_receipt(receiving_client, a, receipt_data(a, lines=[line]))
    assert kinds(first) == ["UNEXPECTED_ITEM"]
    line = line_data(b)
    line["unit"]["identifier"] = {"type": "PCB_SERIAL", "value": "SERIAL-001"}
    second = post_receipt(receiving_client, b, receipt_data(b, lines=[line]))
    assert kinds(second) == ["UNEXPECTED_ITEM"]
    assert first["lines"][0]["asset_id"] != second["lines"][0]["asset_id"]
    assert (
        migrator_connection.execute(
            select(asset_identifiers.c.asset_id).where(
                asset_identifiers.c.type == "PCB_SERIAL", asset_identifiers.c.value == "SERIAL-001"
            )
        )
        .all()
        .__len__()
        == 2
    )


@pytest.mark.parametrize("custodian", ["none", "owner", "vendor"])
def test_explicit_custodian_independent_of_owner_and_vendor(
    custodian, receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    selected = {"none": None, "owner": a.party_id, "vendor": a.vendor_id}[custodian]
    line = line_data(a)
    line["unit"]["custodian_party_id"] = selected
    result = post_receipt(receiving_client, a, receipt_data(a, lines=[line]))
    asset_id = UUID(result["lines"][0]["asset_id"])
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
    assert asset["owner_party_id"] == baseline["initial_owner_party_id"] == a.party_id
    assert asset["custodian_party_id"] == baseline["initial_custodian_party_id"] == selected
    assert asset["created_by_actor_id"] == a.actor_id != a.party_id


@pytest.mark.parametrize(
    "case",
    [
        "missing_owner",
        "null_owner",
        "missing_dock",
        "serialized_no_unit",
        "nonserialized_unit",
        "two_units",
        "fractional_ea",
        "zero",
        "negative",
        "infinity",
        "missing_identifier",
        "blank_value",
        "blank_reason",
        "both_identifier_facts",
        "unknown_identifier",
        "missing_condition",
        "condition_array",
        "condition_bitset",
        "condition_alias",
        "packing_without_reference",
    ],
)
def test_invalid_unit_observations_fail_without_partial_rows(
    case, receiving_client, space_data, make_item, migrator_connection
):
    a = space_data[0]
    payload = receipt_data(a, lines=[line_data(a)])
    line = payload["lines"][0]
    if case == "missing_owner":
        del line["unit"]["owner_party_id"]
    elif case == "null_owner":
        line["unit"]["owner_party_id"] = None
    elif case == "missing_dock":
        payload["dock_location_id"] = None
    elif case == "serialized_no_unit":
        line["unit"] = None
    elif case == "nonserialized_unit":
        line["item_id"] = make_item()
    elif case in {"two_units", "fractional_ea", "zero", "negative", "infinity"}:
        line["quantity"] = {
            "two_units": "2",
            "fractional_ea": "0.5",
            "zero": "0",
            "negative": "-1",
            "infinity": "Infinity",
        }[case]
    elif case == "missing_identifier":
        del line["unit"]["identifier"]
    elif case in {"blank_value", "blank_reason", "both_identifier_facts", "unknown_identifier"}:
        line["unit"]["identifier"] = {
            "type": "UNREADABLE_IDENTIFIER" if case == "unknown_identifier" else "OTHER",
            "value": None if case == "blank_reason" else (" " if case == "blank_value" else "real"),
            "unreadable_reason": " "
            if case == "blank_reason"
            else ("Unreadable" if case == "both_identifier_facts" else None),
        }
    elif case == "missing_condition":
        del line["condition"]
    elif case.startswith("condition_"):
        line["condition"] = {
            "condition_array": ["DAMAGED", "OPENED"],
            "condition_bitset": 3,
            "condition_alias": "DAMAGED_OPENED",
        }[case]
    else:
        line["packing_quantity"] = 9
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post("/receipts", headers=headers(a), json=json_data(payload))
    assert response.status_code == 422, response.text
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize(
    "field",
    [
        "exception_type",
        "exceptions",
        "suppress_exceptions",
        "actor_id",
        "org_id",
        "asset_id",
        "conflicting_asset_id",
        "observed_identifier_type",
        "observed_identifier_value",
        "evidence_ref",
        "client_op_id",
        "correction_id",
        "resolution",
        "remaining_quantity",
    ],
)
@pytest.mark.parametrize("boundary", ["header", "line", "unit", "identifier"])
def test_request_cannot_supply_classification_identity_or_deferred_authority(
    field, boundary, receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    payload = receipt_data(a, lines=[line_data(a)])
    targets = {
        "header": payload,
        "line": payload["lines"][0],
        "unit": payload["lines"][0]["unit"],
        "identifier": payload["lines"][0]["unit"]["identifier"],
    }
    targets[boundary][field] = str(a.actor_id)
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post("/receipts", headers=headers(a), json=json_data(payload))
    assert response.status_code == 422
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize(
    "target", ["vendor", "po", "comparator", "item", "dock", "owner", "custodian"]
)
def test_foreign_tenant_business_references_reject_atomically(
    target, receiving_client, space_data, make_order, migrator_connection
):
    a, b = space_data
    po, expected = make_order()
    other_po, other_expected = make_order(tenant=b)
    payload = receipt_data(a, po_id=po["id"], lines=[line_data(a, po_line_id=expected[0]["id"])])
    if target in {"vendor", "po", "dock"}:
        field, value = {
            "vendor": ("vendor_party_id", b.vendor_id),
            "po": ("po_id", other_po["id"]),
            "dock": ("dock_location_id", b.location_id),
        }[target]
        payload[field] = value
    elif target in {"item", "comparator"}:
        field, value = {
            "item": ("item_id", b.item_id),
            "comparator": ("po_line_id", other_expected[0]["id"]),
        }[target]
        payload["lines"][0][field] = value
    else:
        payload["lines"][0]["unit"][target + "_party_id"] = b.party_id
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post("/receipts", headers=headers(a), json=json_data(payload))
    assert response.status_code == 422, response.text
    assert snapshot(migrator_connection, a.org_id) == before


def test_api_does_not_offer_manual_asset_exception_or_creation_component_routes(receiving_client):
    routes = receiving_client.get("/openapi.json").json()["paths"]
    for path in [
        "/assets",
        "/exceptions",
        "/receipt-lines",
        "/asset-initial-facts",
        "/asset-initial-assignment-facts",
        "/asset-transitions",
    ]:
        assert "post" not in routes.get(path, {})
    assert set(routes["/receipts"]) == {"get", "post"}
    assert set(routes["/receipts/{receipt_id}/reconcile"]) == {"post"}


@pytest.mark.parametrize("credential", [None, "invalid"])
def test_missing_or_invalid_bearer_cannot_receive(credential, receiving_client, space_data):
    a = space_data[0]
    response = receiving_client.post(
        "/receipts",
        headers={} if credential is None else {"Authorization": "Bearer invalid"},
        json=json_data(receipt_data(a, lines=[line_data(a)])),
    )
    assert response.status_code == 401


def test_duplicate_asset_tag_rolls_back_every_line_and_exception_in_full_delivery(
    receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    lines = [line_data(a, condition="DAMAGED"), line_data(a, condition="OPENED")]
    lines[1]["unit"]["asset_tag"] = lines[0]["unit"]["asset_tag"]
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post(
        "/receipts", headers=headers(a), json=json_data(receipt_data(a, lines=lines))
    )
    assert response.status_code == 409
    assert snapshot(migrator_connection, a.org_id) == before


def test_failed_later_line_rolls_back_known_conflict_observation_and_all_anomalies(
    receiving_client, asset_data, migrator_connection
):
    a = asset_data[0]
    known = line_data(a, condition="DAMAGED")
    known["unit"]["identifier"]["value"] = "SERIAL-001"
    invalid = deepcopy(known)
    invalid["quantity"] = 2
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post(
        "/receipts", headers=headers(a), json=json_data(receipt_data(a, lines=[known, invalid]))
    )
    assert response.status_code == 422
    assert snapshot(migrator_connection, a.org_id) == before
