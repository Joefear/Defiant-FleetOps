"""Real authenticated tenants; privileged disposal occurs only after all assertions."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice4 import conftest as space_fixtures
from server.tests.slice5 import conftest as asset_fixtures
from server.tests.slice5.test_authentication_boundary import other_human
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders

space_password_hash = space_fixtures.space_password_hash
space_data = space_fixtures.space_data
space_client = space_fixtures.space_client
seed_asset = asset_fixtures.seed_asset
asset_data = asset_fixtures.asset_data
other_human = other_human
TABLES = (orders, lines)


@pytest.fixture(autouse=True)
def procurement_cleanup(space_data, migrator_connection, app_connection):
    """Owner-only fixture destruction is separate from active-guard migrator proofs.

    Production exposes no cleanup verb. Restore the two guards in the same transaction
    before inherited tenant disposal; failures roll back the temporary DDL as well.
    """
    yield
    app_connection.rollback()
    migrator_connection.rollback()
    with migrator_connection.begin():
        for table in TABLES:
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} DISABLE TRIGGER procurement_10_guard"
            )
        for table in reversed(TABLES):
            migrator_connection.execute(
                table.delete().where(table.c.org_id.in_([t.org_id for t in space_data]))
            )
        for table in TABLES:
            migrator_connection.exec_driver_sql(
                f"ALTER TABLE fleetops.{table.name} ENABLE TRIGGER procurement_10_guard"
            )


def order_values(tenant, **changes):
    """SQL business inputs deliberately omit all server-owned timestamps and state."""
    return (
        dict(
            id=uuid7(),
            org_id=tenant.org_id,
            vendor_party_id=tenant.vendor_id,
            po_number="PO-100",
            created_by_actor_id=tenant.actor_id,
            updated_by_actor_id=tenant.actor_id,
        )
        | changes
    )


def line_values(tenant, po_id, **changes):
    """The runtime never receives INSERT authority over the UOM snapshot."""
    return (
        dict(
            id=uuid7(),
            org_id=tenant.org_id,
            po_id=po_id,
            item_id=tenant.item_id,
            line_number=1,
            quantity="2.125",
            unit_price="3.123456789",
            created_by_actor_id=tenant.actor_id,
            updated_by_actor_id=tenant.actor_id,
        )
        | changes
    )


def write(connection, tenant, statement):
    """Asserted writes use an independently logged-in app role and real credentials."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return connection.execute(statement).mappings().one()


@pytest.fixture
def draft(space_data, app_connection):
    a = space_data[0]
    po = write(app_connection, a, orders.insert().values(order_values(a)).returning(orders))
    line = write(
        app_connection, a, lines.insert().values(line_values(a, po["id"])).returning(lines)
    )
    return po, line


@pytest.fixture
def issued(draft, space_data, app_connection):
    po, line = draft
    po = write(
        app_connection,
        space_data[0],
        orders.update().where(orders.c.id == po["id"]).values(status="ISSUED").returning(orders),
    )
    return po, line


def snapshot(connection, table, row_id):
    """Read every stored column, including attribution and record timestamps."""
    row = dict(connection.execute(select(table).where(table.c.id == row_id)).mappings().one())
    connection.rollback()
    return row


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}
