"""Real runtime receiving fixtures with separately privileged, tenant-scoped disposal."""

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from server.tests.auth_context import set_authenticated
from server.tests.slice4 import conftest as space_fixtures
from server.tests.slice5 import conftest as asset_fixtures
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.db.metadata import (
    asset_assignment_events,
    asset_configurations,
    asset_custody_changes,
    asset_identifiers,
    asset_initial_assignment_facts,
    asset_initial_facts,
    asset_movements,
    asset_ownership_changes,
    asset_transitions,
    assets,
    items,
    purchase_order_lines,
    purchase_orders,
    receipt_comparators,
    receipt_lines,
    receipt_reconciliations,
    receipts,
    receiving_exceptions,
)
from fleetops.domain import procurement

space_password_hash = space_fixtures.space_password_hash
space_data = space_fixtures.space_data
seed_asset = asset_fixtures.seed_asset
asset_data = asset_fixtures.asset_data

RECEIVING_TABLES = (
    receipts,
    receipt_comparators,
    receipt_lines,
    receipt_reconciliations,
    receiving_exceptions,
)
ASSET_TABLES = (
    assets,
    asset_identifiers,
    asset_transitions,
    asset_initial_facts,
    asset_initial_assignment_facts,
    asset_assignment_events,
    asset_configurations,
    asset_movements,
    asset_custody_changes,
    asset_ownership_changes,
)
WHEN = datetime(2026, 9, 11, 15, tzinfo=UTC)


@pytest.fixture(autouse=True)
def receiving_cleanup(space_data, migrator_connection, app_connection):
    """Disable only owned immutable guards during privileged test disposal, then restore them."""
    yield
    app_connection.rollback()
    migrator_connection.rollback()
    orgs = [tenant.org_id for tenant in space_data]
    with migrator_connection.begin():
        for table in RECEIVING_TABLES:
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} DISABLE TRIGGER receiving_10_guard"
            )
        for table in reversed(RECEIVING_TABLES):
            migrator_connection.execute(table.delete().where(table.c.org_id.in_(orgs)))
        for table in RECEIVING_TABLES:
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} ENABLE TRIGGER receiving_10_guard"
            )
        for table in (purchase_orders, purchase_order_lines):
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} DISABLE TRIGGER procurement_10_guard"
            )
        for table in (purchase_order_lines, purchase_orders):
            migrator_connection.execute(table.delete().where(table.c.org_id.in_(orgs)))
        for table in (purchase_orders, purchase_order_lines):
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} ENABLE TRIGGER procurement_10_guard"
            )


@pytest.fixture
def receiving_client(database, space_data):
    with TestClient(create_app(database.settings(space_data[0].org_id))) as client:
        yield client


@pytest.fixture
def make_item(space_data, migrator_connection):
    """Catalog setup supplies independent actual/expected serialization and UOM facts."""

    def create(*, tenant=None, serialized=False, uom="EA"):
        tenant = tenant or space_data[0]
        item_id = uuid7()
        migrator_connection.execute(
            items.insert().values(
                id=item_id,
                org_id=tenant.org_id,
                manufacturer_party_id=tenant.party_id,
                manufacturer_part_number=str(item_id),
                revision="A",
                description="Receipt fixture",
                uom=uom,
                serialized=serialized,
                created_by_actor_id=tenant.actor_id,
                updated_by_actor_id=tenant.actor_id,
            )
        )
        migrator_connection.commit()
        return item_id

    return create


@pytest.fixture
def make_order(space_data, app_connection):
    """Create expectation through the real runtime procurement boundary, then freeze it."""

    def create(*, tenant=None, specs=None, issue=True):
        tenant = tenant or space_data[0]
        specs = specs or [{"item_id": tenant.item_id, "quantity": 1}]
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            order = dict(
                procurement.create_order(
                    app_connection,
                    org_id=tenant.org_id,
                    performer_id=tenant.actor_id,
                    values={
                        "vendor_party_id": tenant.vendor_id,
                        "po_number": str(uuid7()),
                        "notes": None,
                    },
                )
            )
            lines = []
            for number, spec in enumerate(specs, 1):
                lines.append(
                    dict(
                        procurement.create_line(
                            app_connection,
                            order["id"],
                            performer_id=tenant.actor_id,
                            values={
                                "line_number": number,
                                "unit_price": "123.123456789",
                                "expected_date": None,
                            }
                            | spec,
                        )
                    )
                )
            if issue:
                order = dict(procurement.issue_order(app_connection, order["id"]))
        return order, lines

    return create


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def line_data(tenant, **changes):
    """A unit has an explicit owner different from vendor and authenticated Actor."""
    return {
        "po_line_id": None,
        "item_id": tenant.item_id,
        "quantity": "1",
        "uom": "EA",
        "condition": "GOOD",
        "packing_quantity": None,
        "notes": None,
        "unit": {
            "owner_party_id": tenant.party_id,
            "custodian_party_id": None,
            "asset_tag": str(uuid7()),
            "description": "Received workstation",
            "identifier": {
                "type": "MANUFACTURER_SERIAL",
                "value": str(uuid7()),
                "unreadable_reason": None,
            },
        },
    } | changes


def receipt_data(tenant, **changes):
    return {
        "vendor_party_id": tenant.vendor_id,
        "po_id": None,
        "dock_location_id": tenant.location_id,
        "packing_reference": None,
        "received_at": WHEN,
        "comparator_ids": [],
        "lines": [],
        "reconcile": True,
    } | changes


def json_data(value):
    return json.loads(json.dumps(value, default=str))


def post_receipt(client, tenant, payload):
    response = client.post("/receipts", headers=headers(tenant), json=json_data(payload))
    assert response.status_code == 201, response.text
    return response.json()


def kinds(receipt):
    return sorted(row["exception_type"] for row in receipt["exceptions"])


def snapshot(connection, org_id, tables=RECEIVING_TABLES + ASSET_TABLES):
    """Independent committed-row oracle, including every relevant projection/history column."""
    result = {
        table.name: connection.execute(
            select(table).where(table.c.org_id == org_id).order_by(*table.primary_key.columns)
        ).all()
        for table in tables
    }
    connection.rollback()
    return result


UNIT_CALL = """
SELECT * FROM fleetops.create_received_unit(
    :receipt_id,:line_id,:po_line_id,:item_id,:quantity,:uom,:condition,
    :packing_quantity,:notes,:asset_id,:asset_tag,:description,
    :owner_party_id,:custodian_party_id,:identifier_id,:identifier_type,
    :identifier_value,:unreadable_reason,:transition_id)
"""


def unit_parameters(tenant, receipt_id, **changes):
    """Declared normal-admission inputs for direct runtime SQL; no service code is used."""
    return {
        "receipt_id": receipt_id,
        "line_id": uuid7(),
        "po_line_id": None,
        "item_id": tenant.item_id,
        "quantity": 1,
        "uom": "EA",
        "condition": "GOOD",
        "packing_quantity": None,
        "notes": None,
        "asset_id": uuid7(),
        "asset_tag": str(uuid7()),
        "description": "Direct received unit",
        "owner_party_id": tenant.party_id,
        "custodian_party_id": None,
        "identifier_id": uuid7(),
        "identifier_type": "MANUFACTURER_SERIAL",
        "identifier_value": str(uuid7()),
        "unreadable_reason": None,
        "transition_id": uuid7(),
    } | changes


def exception_values(tenant, receipt_id, kind, **changes):
    """Explicit expected relationships for raw SQL tests; no classification derivation."""
    return {
        "id": uuid7(),
        "org_id": tenant.org_id,
        "receipt_id": receipt_id,
        "exception_type": kind,
        "receipt_line_id": None,
        "po_line_id": None,
        "asset_id": None,
        "conflicting_asset_id": None,
    } | changes
