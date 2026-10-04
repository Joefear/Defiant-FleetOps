"""Retaining pinned receipt authority differs from selecting a new comparator (ADR-013)."""

from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)
from server.tests.slice10.test_receipt_boundaries import correct, effective
from sqlalchemy import select

from fleetops.db import metadata as db
from fleetops.domain import procurement

CONSEQUENCES = (
    db.receipt_lines,
    db.receipt_comparators,
    db.receipt_line_corrections,
    db.receipt_correction_evaluations,
    db.receipt_evaluation_lines,
    db.receipt_evaluation_expectations,
    db.receipt_evaluation_exceptions,
    db.receiving_exceptions,
    db.exception_workflows,
    db.exception_events,
)


def amend(connection, tenant, po, line, item):
    """A genuine later expectation change leaves the admitted receipt version historical."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return procurement.create_line(
            connection,
            po["id"],
            performer_id=tenant.actor_id,
            predecessor_id=line["id"],
            values=dict(item_id=item, quantity=2, unit_price=30, expected_date=None),
        )


@pytest.mark.parametrize("origin", ["original", "corrected"])
@pytest.mark.parametrize("explicit_comparator", [False, True])
def test_retained_superseded_comparator_preserves_exact_source(
    origin,
    explicit_comparator,
    space_data,
    make_item,
    make_order,
    receiving_client,
    app_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1), dict(item_id=item, quantity=1)])
    target = lines[0 if origin == "original" else 1]
    corrected_po = receiving_client.post(
        f"/purchase-orders/{po['id']}/lines/{target['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                reason="Correct recorded price",
                correction_occurred_at=WHEN,
                unit_price="20",
            )
        ),
    )
    assert corrected_po.status_code == 201, corrected_po.text
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
                    po_line_id=lines[0]["id"],
                    expected_po_generation=1 if origin == "original" else 0,
                    unit=None,
                )
            ],
        ),
    )
    generation = 0
    if origin == "corrected":
        selected = correct(
            receiving_client,
            tenant,
            captured,
            po_line_id=target["id"],
            expected_po_generation=1,
        )
        assert selected.status_code == 201, selected.text
        generation = 1
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        bindings = (
            app_connection.execute(
                select(db.receipt_comparators)
                .where(db.receipt_comparators.c.receipt_id == UUID(captured["id"]))
                .order_by(db.receipt_comparators.c.id)
            )
            .mappings()
            .all()
        )
    target_binding = next(row for row in bindings if row["po_line_id"] == target["id"])
    assert target_binding["source_generation"] == 1
    assert target_binding["source_id"] == UUID(corrected_po.json()["id"])
    amend(app_connection, tenant, po, target, item)

    # Both an omitted comparator and an explicit unchanged one retain pinned authority.
    changes = {"po_line_id": target["id"]} if explicit_comparator else {}
    result = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=generation,
        notes="Corrected note after later supplier amendment",
        **changes,
    )
    assert result.status_code == 201, result.text
    current = effective(receiving_client, tenant, captured)
    assert current["root"] == captured["lines"][0]
    assert current["correction_generation"] == generation + 1
    assert current["effective"]["po_line_id"] == str(target["id"])
    assert current["effective"]["notes"] == "Corrected note after later supplier amendment"
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert (
            app_connection.execute(
                select(db.receipt_comparators)
                .where(db.receipt_comparators.c.receipt_id == UUID(captured["id"]))
                .order_by(db.receipt_comparators.c.id)
            )
            .mappings()
            .all()
            == bindings
        )
    health = receiving_client.get("/health/corrections", headers=headers(tenant))
    assert health.status_code == 200 and health.json() == []


@pytest.mark.parametrize("prior_selection", ["unbound", "previously_bound", "cleared"])
def test_different_superseded_comparator_rejects_without_consequences(
    prior_selection,
    space_data,
    make_item,
    make_order,
    receiving_client,
    app_connection,
    migrator_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1), dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[line_data(tenant, item_id=item, po_line_id=lines[0]["id"], unit=None)],
        ),
    )
    generation = 0
    if prior_selection != "unbound":
        result = correct(
            receiving_client,
            tenant,
            captured,
            po_line_id=lines[1]["id"] if prior_selection == "previously_bound" else None,
        )
        assert result.status_code == 201, result.text
        generation = 1
    target = lines[1 if prior_selection == "unbound" else 0]
    successor = amend(app_connection, tenant, po, target, item)
    before = snapshot(migrator_connection, tenant.org_id, CONSEQUENCES)
    result = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=generation,
        po_line_id=target["id"],
    )
    assert result.status_code == 409, result.text
    assert snapshot(migrator_connection, tenant.org_id, CONSEQUENCES) == before
    # A historical binding (or the original root after clearing) cannot waive new admission.
    admitted = correct(
        receiving_client,
        tenant,
        captured,
        expected_generation=generation,
        po_line_id=successor["id"],
    )
    assert admitted.status_code == 201, admitted.text
    assert effective(receiving_client, tenant, captured)["effective"]["po_line_id"] == str(
        successor["id"]
    )
