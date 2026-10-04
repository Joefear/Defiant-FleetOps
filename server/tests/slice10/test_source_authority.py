"""Runtime source pin, evaluation linkage, and integer/time authority remain durable."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
)
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db import metadata as db
from fleetops.domain import record_corrections


@pytest.mark.parametrize(
    "field,value",
    [
        ("correction_generation", 2),
        ("correction_generation", 2147483648),
        ("correction_occurred_at", "infinity"),
        ("reason", "\u00a0\u2007"),
    ],
)
def test_record_generation_time_reason_reject_at_raw_runtime_boundary(
    field,
    value,
    space_data,
    make_item,
    make_order,
    app_connection,
):
    tenant = space_data[0]
    po, lines = make_order(specs=[dict(item_id=make_item(), quantity=1)])
    original = lines[0]
    values = {name: original[name] for name in record_corrections.PO_FIELDS}
    values.update(
        id=uuid7(),
        org_id=tenant.org_id,
        po_id=po["id"],
        po_line_id=original["id"],
        correction_role="REVERSAL",
        correction_pair_id=uuid7(),
        correction_generation=1,
        correction_occurred_at=WHEN,
        occurred_at=WHEN,
        reason="Required reason",
    )
    values[field] = value
    with pytest.raises(DBAPIError) as denied:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            app_connection.execute(db.purchase_order_line_corrections.insert().values(values))
    assert denied.value.orig.sqlstate in ("40001", "22003", "23514")
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert app_connection.execute(select(db.purchase_order_line_corrections.c.id)).all() == []


@pytest.mark.parametrize("source_kind", ["reversal", "foreign_root", "wrong_generation"])
def test_receipt_comparator_cannot_forge_corrected_source(
    source_kind, space_data, make_item, make_order, app_connection
):
    from fleetops.domain import receiving

    tenant = space_data[0]
    po, lines = make_order(
        specs=[dict(item_id=make_item(), quantity=1), dict(item_id=make_item(), quantity=1)]
    )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        corrected = record_corrections.correct_procurement(
            app_connection,
            po["id"],
            lines[0]["id"],
            values=dict(
                expected_generation=0,
                quantity=2,
                reason="Expectation transcription",
                correction_occurred_at=WHEN,
            ),
        )
        reversal = app_connection.execute(
            select(db.purchase_order_line_corrections.c.id).where(
                db.purchase_order_line_corrections.c.correction_pair_id
                == corrected["correction_pair_id"],
                db.purchase_order_line_corrections.c.correction_role == "REVERSAL",
            )
        ).scalar_one()
        captured = receiving.create_receipt(
            app_connection,
            org_id=tenant.org_id,
            values=receipt_data(tenant, po_id=po["id"], reconcile=False),
        )
    with pytest.raises(DBAPIError) as denied:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            app_connection.execute(
                db.receipt_comparators.insert().values(
                    id=uuid7(),
                    org_id=tenant.org_id,
                    receipt_id=captured["id"],
                    po_line_id=lines[1 if source_kind == "foreign_root" else 0]["id"],
                    source_id=reversal if source_kind == "reversal" else corrected["id"],
                    source_generation=2 if source_kind == "wrong_generation" else 1,
                )
            )
    assert denied.value.orig.sqlstate in ("23503", "23514", "40001")
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert app_connection.execute(select(db.receipt_comparators.c.id)).all() == []


def test_committed_evaluation_cannot_accept_later_cross_context_consequence(
    space_data,
    make_item,
    make_order,
    receiving_client,
    app_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = []
    for _ in range(2):
        captured.append(
            post_receipt(
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
                            unit=None,
                            condition="DAMAGED",
                        )
                    ],
                ),
            )
        )
    first = captured[0]
    result = receiving_client.post(
        f"/receipts/{first['id']}/lines/{first['lines'][0]['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                condition="GOOD",
                reason="Correct inspection",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert result.status_code == 201, result.text
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        evaluation = app_connection.execute(
            select(db.receipt_correction_evaluations.c.id)
        ).scalar_one()
    with pytest.raises(DBAPIError) as denied:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            app_connection.execute(
                db.receipt_evaluation_exceptions.insert().values(
                    id=uuid7(),
                    org_id=tenant.org_id,
                    evaluation_id=evaluation,
                    exception_id=captured[1]["exceptions"][0]["id"],
                    supported=False,
                    prior_event_seq=0,
                    occurred_at=WHEN,
                )
            )
    assert denied.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "field",
    [
        "actor_id",
        "org_id",
        "event_seq",
        "recorded_at",
        "evaluation_id",
        "correction_pair_id",
        "status",
        "resolved_by_actor_id",
        "resolved_at",
        "resolution_note",
    ],
)
def test_workflow_api_rejects_caller_authority(field, space_data, receiving_client):
    tenant = space_data[0]
    response = receiving_client.post(
        f"/exceptions/{uuid7()}/events",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_status="OPEN",
                to_status="RESOLVED",
                note="Disposition",
                occurred_at=WHEN,
                **{field: "forged"},
            )
        ),
    )
    assert response.status_code == 422, response.text
    assert any(
        error["type"] == "extra_forbidden" and error["loc"][-1] == field
        for error in response.json()["detail"]
    )
