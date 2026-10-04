"""Receipt corrections retain creation identity, exact comparator populations and UOM claims."""

import pytest
from server.tests.slice9.conftest import (
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
)


def correct(client, tenant, captured, **values):
    return client.post(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                reason="Correct receiving observation",
                correction_occurred_at=WHEN,
            )
            | values
        ),
    )


def effective(client, tenant, captured):
    response = client.get(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/effective",
        headers=headers(tenant),
    )
    assert response.status_code == 200, response.text
    return response.json()


def open_for(client, tenant, receipt):
    response = client.get(
        "/exceptions/open", headers=headers(tenant), params={"receipt_id": receipt}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_comparator_change_evaluates_both_populations_and_preserves_commitments(
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    first, second = make_item(), make_item()
    po, lines = make_order(
        specs=[dict(item_id=first, quantity=3), dict(item_id=second, quantity=2)]
    )
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(tenant, item_id=first, quantity="3", po_line_id=lines[0]["id"], unit=None)
            ],
        ),
    )
    response = correct(
        receiving_client,
        tenant,
        captured,
        po_line_id=lines[1]["id"],
        item_id=second,
        quantity="2",
        notes="Verified label",
    )
    assert response.status_code == 201, response.text
    current = effective(receiving_client, tenant, captured)
    assert current["root"] == captured["lines"][0]
    assert current["effective"]["po_line_id"] == str(lines[1]["id"])
    opened = open_for(receiving_client, tenant, captured["id"])
    assert [(row["exception_type"], row["po_line_id"]) for row in opened] == [
        ("SHORT", str(lines[0]["id"]))
    ]
    repeated = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=1,
        po_line_id=lines[0]["id"],
        item_id=first,
        quantity="3",
    )
    assert repeated.status_code == 201, repeated.text
    assert effective(receiving_client, tenant, captured)["effective"]["notes"] == "Verified label"
    readback = receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()
    assert set(readback["comparator_ids"]) == {str(row["id"]) for row in lines}
    assert readback["lines"] == captured["lines"]
    opened = open_for(receiving_client, tenant, captured["id"])
    assert [(row["exception_type"], row["po_line_id"]) for row in opened] == [
        ("SHORT", str(lines[1]["id"]))
    ]
    assert receiving_client.get("/health/corrections", headers=headers(tenant)).json() == []


def test_mismatched_uom_blocks_entire_comparator_and_other_receipts_never_net(
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=4)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(tenant, item_id=item, quantity="1", po_line_id=lines[0]["id"], unit=None)
            ],
        ),
    )
    original_short = captured["exceptions"][0]["id"]
    other = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, quantity="1000", po_line_id=lines[0]["id"], unit=None
                )
            ],
        ),
    )
    response = correct(receiving_client, tenant, captured, uom="M", quantity="1000")
    assert response.status_code == 201, response.text
    assert [
        row["exception_type"] for row in open_for(receiving_client, tenant, captured["id"])
    ] == ["UOM_MISMATCH"]
    repeated = correct(
        receiving_client, tenant, captured, expected_generation=1, uom="EA", quantity="2"
    )
    assert repeated.status_code == 201, repeated.text
    opened = open_for(receiving_client, tenant, captured["id"])
    assert [row["exception_type"] for row in opened] == ["SHORT"]
    assert opened[0]["id"] != original_short
    assert receiving_client.get(f"/receipts/{other['id']}", headers=headers(tenant)).json() == other
    assert receiving_client.get("/health/corrections", headers=headers(tenant)).json() == []


@pytest.mark.parametrize(
    "change", ["quantity", "item", "observation", "nonserialized_to_serialized"]
)
def test_creation_changing_replacements_reject_atomically(
    change,
    space_data,
    make_item,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item(serialized=True)
    values = line_data(tenant)
    if change == "nonserialized_to_serialized":
        values = line_data(tenant, item_id=make_item(), unit=None)
    captured = post_receipt(receiving_client, tenant, receipt_data(tenant, lines=[values]))
    replacement = {"quantity": "2"} if change == "quantity" else {"item_id": item}
    if change == "observation":
        replacement = dict(
            observed_identifier_type="MANUFACTURER_SERIAL", observed_identifier_value="new serial"
        )
    response = correct(receiving_client, tenant, captured, **replacement)
    assert response.status_code == 422, response.text
    assert effective(receiving_client, tenant, captured)["correction_generation"] == 0
    assert (
        receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()
        == captured
    )


def test_known_conflict_identifier_correction_stays_observation_only(space_data, receiving_client):
    tenant = space_data[0]
    first_line = line_data(tenant)
    first = post_receipt(receiving_client, tenant, receipt_data(tenant, lines=[first_line]))
    second_line = line_data(tenant)
    second_line["unit"]["identifier"] = dict(first_line["unit"]["identifier"])
    conflict = post_receipt(receiving_client, tenant, receipt_data(tenant, lines=[second_line]))
    third_line = line_data(tenant)
    third = post_receipt(receiving_client, tenant, receipt_data(tenant, lines=[third_line]))
    assets_before = receiving_client.get("/assets", headers=headers(tenant)).json()
    invalid = correct(
        receiving_client,
        tenant,
        conflict,
        observed_identifier_type=None,
        observed_identifier_value=None,
    )
    assert invalid.status_code == 422, invalid.text
    changed = correct(
        receiving_client,
        tenant,
        conflict,
        observed_identifier_type="MANUFACTURER_SERIAL",
        observed_identifier_value=third_line["unit"]["identifier"]["value"],
    )
    assert changed.status_code == 201, changed.text
    assert changed.json()["asset_id"] is None
    assert changed.json()["conflicting_asset_id"] == third["lines"][0]["asset_id"]
    assert changed.json()["conflicting_asset_id"] != first["lines"][0]["asset_id"]
    assert receiving_client.get("/assets", headers=headers(tenant)).json() == assets_before
    opened = open_for(receiving_client, tenant, conflict["id"])
    assert {row["exception_type"] for row in opened} == {"UNEXPECTED_ITEM", "SERIAL_MISMATCH"}
    assert receiving_client.get("/health/corrections", headers=headers(tenant)).json() == []


def test_uncompleted_receipt_is_not_correctable(space_data, make_item, receiving_client):
    tenant = space_data[0]
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant, reconcile=False, lines=[line_data(tenant, item_id=make_item(), unit=None)]
        ),
    )
    response = correct(receiving_client, tenant, captured, quantity="2")
    assert response.status_code == 409, response.text
    assert (
        receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()
        == captured
    )


@pytest.mark.parametrize("introduced_by_correction", [False, True])
@pytest.mark.parametrize("procurement_generation", [0, 1])
def test_retained_comparator_survives_supersession_but_reselection_requires_active_leaf(
    introduced_by_correction,
    procurement_generation,
    space_data,
    make_item,
    make_order,
    receiving_client,
    app_connection,
):
    """ADR-013 D34: retention preserves a commitment; selecting another target re-admits it."""
    from server.tests.auth_context import set_authenticated

    from fleetops.domain import procurement, record_corrections

    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=2), dict(item_id=item, quantity=2)])
    target = lines[1]["id"]
    if procurement_generation:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            record_corrections.correct_procurement(
                app_connection,
                po["id"],
                target,
                values=dict(
                    expected_generation=0,
                    quantity=3,
                    reason="Correct expected count",
                    correction_occurred_at=WHEN,
                ),
            )
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant,
                    item_id=item,
                    quantity="1",
                    unit=None,
                    po_line_id=lines[0]["id"] if introduced_by_correction else target,
                    expected_po_generation=0
                    if introduced_by_correction
                    else procurement_generation,
                )
            ],
        ),
    )
    generation = 0
    if introduced_by_correction:
        response = correct(
            receiving_client,
            tenant,
            captured,
            po_line_id=target,
            expected_po_generation=procurement_generation,
        )
        assert response.status_code == 201, response.text
        generation += 1
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        successor = procurement.create_line(
            app_connection,
            po["id"],
            performer_id=tenant.actor_id,
            predecessor_id=target,
            values=dict(item_id=item, quantity=9, unit_price=10, expected_date=None),
        )
    # Both omitted and explicit unchanged targets retain the frozen comparator source.
    for target_patch in ({}, {"po_line_id": target}):
        response = correct(
            receiving_client,
            tenant,
            captured,
            expected_generation=generation,
            quantity=str(2 + procurement_generation),
            **target_patch,
        )
        assert response.status_code == 201, response.text
        generation += 1
        current = effective(receiving_client, tenant, captured)
        assert current["root"] == captured["lines"][0]
        assert current["effective"]["po_line_id"] == str(target)
        assert not any(
            row["po_line_id"] == str(target) and row["exception_type"] in ("SHORT", "OVER")
            for row in open_for(receiving_client, tenant, captured["id"])
        )
    response = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=generation,
        po_line_id=successor["id"],
    )
    assert response.status_code == 201, response.text
    generation += 1
    before = effective(receiving_client, tenant, captured)
    # A historical binding is not permission to select an inactive target again.
    rejected = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=generation,
        po_line_id=target,
        expected_po_generation=procurement_generation,
    )
    assert rejected.status_code == 409, rejected.text
    assert effective(receiving_client, tenant, captured) == before
    assert receiving_client.get("/health/corrections", headers=headers(tenant)).json() == []
