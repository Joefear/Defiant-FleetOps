"""Logical Exception fields retain immutable receiving truth and bounded vocabulary."""

import re

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
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from fleetops.db.metadata import exception_workflows, receiving_exceptions
from fleetops.domain.exception_types import ExceptionType
from fleetops.domain.receiving_types import ReceivingExceptionType


@pytest.fixture
def observations(space_data, make_item, make_order, receiving_client):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=2)])
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
                    unit=None,
                    condition="DAMAGED",
                )
            ],
        ),
    )
    return captured


def test_logical_fields_and_primary_entity_filter(
    observations,
    space_data,
    receiving_client,
    app_connection,
):
    tenant = space_data[0]
    captured = observations
    for observation in captured["exceptions"]:
        identifier = observation["id"]
        response = receiving_client.get(f"/exceptions/{identifier}", headers=headers(tenant))
        assert response.status_code == 200, response.text
        result = response.json()
        for key, value in observation.items():
            assert result[key] == value
        assert result["severity"] == "UNSPECIFIED"
        assert result["opened_by_actor_id"] == observation["actor_id"]
        assert result["opened_at"] == observation["occurred_at"]
        assert result["status"] == "OPEN" and result["event_seq"] == 0
        assert result["events"] == []
        expected_type = "RECEIPT_LINE" if observation["receipt_line_id"] else "RECEIPT"
        expected_id = observation["receipt_line_id"] or captured["id"]
        assert (result["entity_type"], result["entity_id"]) == (expected_type, expected_id)
        filtered = receiving_client.get(
            "/exceptions/open",
            headers=headers(tenant),
            params=dict(entity_type=expected_type, entity_id=expected_id),
        )
        assert filtered.status_code == 200, filtered.text
        assert [row["id"] for row in filtered.json()] == [identifier]
        foreign = receiving_client.get(
            "/exceptions/open",
            headers=headers(space_data[1]),
            params=dict(entity_type=expected_type, entity_id=expected_id),
        )
        assert foreign.status_code == 200 and foreign.json() == []
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            row = (
                app_connection.execute(
                    select(exception_workflows).where(
                        exception_workflows.c.exception_id == identifier,
                    )
                )
                .mappings()
                .one()
            )
            assert row["exception_type"] == observation["exception_type"]
            assert row["severity"] == result["severity"]
            assert str(row["entity_id"]) == expected_id
    for params in (
        dict(entity_type="RECEIPT"),
        dict(entity_id=captured["id"]),
        dict(entity_type="ASSET", entity_id=captured["id"]),
    ):
        response = receiving_client.get("/exceptions/open", headers=headers(tenant), params=params)
        assert response.status_code == 422, response.text


def test_workflow_vocabulary_is_not_receiving_production(
    migrator_connection,
    receiving_client,
    space_data,
):
    constraints = dict(
        migrator_connection.execute(
            text("""
        SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname IN ('ck_exception_workflows_type','ck_receiving_exceptions_type')
    """)
        ).all()
    )
    migrator_connection.rollback()
    workflow = set(re.findall("'([A-Z_]+)'", constraints["ck_exception_workflows_type"]))
    receiving = set(re.findall("'([A-Z_]+)'", constraints["ck_receiving_exceptions_type"]))
    assert receiving == {kind.value for kind in ReceivingExceptionType}
    assert workflow == receiving | {"GENERAL", "SYNC_CONFLICT", "MISSING_LOT", "BALANCE_NEGATIVE"}
    assert workflow == {kind.value for kind in ExceptionType}
    schema = receiving_client.get("/openapi.json").json()
    assert set(schema["components"]["schemas"]["ExceptionType"]["enum"]) == workflow
    assert "/exceptions" not in schema["paths"]
    for kind in workflow - receiving:
        response = receiving_client.post(
            "/receipts",
            headers=headers(space_data[0]),
            json=json_data(
                {
                    **receipt_data(space_data[0]),
                    "received_at": WHEN.isoformat(),
                    "exception_type": kind,
                }
            ),
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize("role", ["app", "migrator"])
@pytest.mark.parametrize(
    "field",
    [
        "exception_id",
        "org_id",
        "exception_type",
        "severity",
        "entity_type",
        "entity_id",
    ],
)
def test_logical_observation_fields_are_immutable(
    role,
    field,
    observations,
    space_data,
    app_connection,
    migrator_connection,
):
    tenant = space_data[0]
    identifier = observations["exceptions"][0]["id"]
    connection = app_connection if role == "app" else migrator_connection
    with pytest.raises(DBAPIError) as raised:
        with connection.begin():
            if role == "app":
                set_authenticated(connection, tenant)
            connection.execute(
                text(
                    f"UPDATE fleetops.exception_workflows SET {field}={field} "
                    "WHERE exception_id=:id"
                ),
                dict(id=identifier),
            )
    assert raised.value.orig.sqlstate in {"42501", "23514"}
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert (
            app_connection.execute(
                select(receiving_exceptions.c.id).where(
                    receiving_exceptions.c.id == identifier,
                )
            ).scalar_one()
            is not None
        )
