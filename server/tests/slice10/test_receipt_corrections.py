"""Receipt corrections preserve capture while synchronously reconciling Exception authority."""

import pytest
from server.tests.slice9.conftest import (
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
)


def correction(client, tenant, receipt, line, **changes):
    return client.post(
        f"/receipts/{receipt}/lines/{line}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                reason="Correct inspected condition",
                correction_occurred_at=WHEN,
                **changes,
            )
        ),
    )


@pytest.mark.parametrize("prior_status", ["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"])
def test_false_observation_resolves_or_preserves_terminal(
    prior_status,
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], condition="DAMAGED", unit=None
                )
            ],
        ),
    )
    original = captured["lines"][0]
    observation = captured["exceptions"][0]
    assert observation["exception_type"] == "DAMAGED"
    if prior_status != "OPEN":
        response = receiving_client.post(
            f"/exceptions/{observation['id']}/events",
            headers=headers(tenant),
            json=json_data(
                dict(
                    expected_status="OPEN",
                    to_status=prior_status,
                    note="Inspection disposition",
                    occurred_at=WHEN,
                )
            ),
        )
        assert response.status_code == 201, response.text
    before = receiving_client.get(
        f"/exceptions/{observation['id']}", headers=headers(tenant)
    ).json()
    response = correction(
        receiving_client, tenant, captured["id"], original["id"], condition="GOOD"
    )
    assert response.status_code == 201, response.text
    after = receiving_client.get(f"/exceptions/{observation['id']}", headers=headers(tenant)).json()
    for field, value in observation.items():
        assert after[field] == value
    if prior_status in ("RESOLVED", "WAIVED"):
        assert after == before
    else:
        assert after["status"] == "RESOLVED"
        assert len(after["events"]) == len(before["events"]) + 1
        assert after["events"][-1]["evaluation_id"] is not None
        # D11/D42: explain the automatic resolution and reference the immutable
        # submitted reason without presenting server prose as an operator quote.
        expected_note = (
            "Corrected receipt reality superseded this observation's assertion. "
            "Reason: see receipt-line correction pair "
            f"{response.json()['correction_pair_id']}."
        )
        assert after["resolution_note"] == expected_note
        assert after["events"][-1]["note"] == expected_note
    readback = receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()
    assert readback["lines"][0] == original
    history = receiving_client.get(
        f"/receipts/{captured['id']}/lines/{original['id']}/corrections", headers=headers(tenant)
    ).json()
    assert len(history) == 2
    assert {row["correction_role"] for row in history} == {"REVERSAL", "CORRECTED"}
    assert {row["reason"] for row in history} == {"Correct inspected condition"}
    assert {row["correction_pair_id"] for row in history} == {response.json()["correction_pair_id"]}
    health = receiving_client.get("/health/corrections", headers=headers(tenant))
    assert health.status_code == 200, health.text
    assert health.json() == []


def test_reappearing_disagreement_creates_new_open_instance(
    space_data, make_item, make_order, receiving_client
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], condition="DAMAGED", unit=None
                )
            ],
        ),
    )
    root = captured["lines"][0]["id"]
    first = correction(receiving_client, tenant, captured["id"], root, condition="GOOD")
    assert first.status_code == 201, first.text
    second = receiving_client.post(
        f"/receipts/{captured['id']}/lines/{root}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=1,
                reason="Second inspection",
                correction_occurred_at=WHEN,
                condition="DAMAGED",
            )
        ),
    )
    assert second.status_code == 201, second.text
    observed = receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()[
        "exceptions"
    ]
    assert len(observed) == 2
    old = receiving_client.get(
        f"/exceptions/{captured['exceptions'][0]['id']}", headers=headers(tenant)
    ).json()
    assert old["status"] == "RESOLVED"
    opened = receiving_client.get(
        "/exceptions/open", headers=headers(tenant), params={"receipt_id": captured["id"]}
    ).json()
    assert len(opened) == 1
    assert opened[0]["id"] != old["id"]
    assert opened[0]["status"] == "OPEN"


def test_procurement_correction_binding_requires_generation_and_then_freezes(
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=2)])
    root = lines[0]["id"]
    url = f"/purchase-orders/{po['id']}/lines/{root}/corrections"
    response = receiving_client.post(
        url,
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                reason="Transcription mistake",
                correction_occurred_at=WHEN,
                quantity="3",
            )
        ),
    )
    assert response.status_code == 201, response.text
    delivery = receipt_data(tenant, po_id=po["id"], comparator_ids=[root])
    stale = receiving_client.post("/receipts", headers=headers(tenant), json=json_data(delivery))
    assert stale.status_code == 409, stale.text
    delivery["comparator_ids"] = []
    delivery["comparator_bindings"] = [dict(po_line_id=root, expected_generation=1)]
    accepted = post_receipt(receiving_client, tenant, delivery)
    assert [e["exception_type"] for e in accepted["exceptions"]] == ["SHORT"]
    frozen = receiving_client.post(
        url,
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=1,
                reason="Price correction",
                correction_occurred_at=WHEN,
                unit_price="5",
            )
        ),
    )
    assert frozen.status_code == 409, frozen.text


def test_receipt_correction_database_boundary(space_data, make_item, make_order, app_connection):
    """Exercise the runtime service without API error translation hiding a durable failure."""
    from server.tests.auth_context import set_authenticated

    from fleetops.domain import receiving, record_corrections

    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        captured = receiving.create_receipt(
            app_connection,
            org_id=tenant.org_id,
            values=receipt_data(
                tenant,
                po_id=po["id"],
                lines=[
                    line_data(
                        tenant,
                        item_id=item,
                        po_line_id=lines[0]["id"],
                        condition="DAMAGED",
                        unit=None,
                    )
                ],
            ),
        )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        result = record_corrections.correct_receipt_line(
            app_connection,
            captured["id"],
            captured["lines"][0]["id"],
            values=dict(
                expected_generation=0,
                reason="Inspection correction",
                correction_occurred_at=WHEN,
                condition="GOOD",
            ),
        )
        assert result["correction_generation"] == 1
