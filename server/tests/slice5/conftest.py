"Controlled Asset creation exists only in tests; runtime assertions use real PostgreSQL."

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from server.tests.auth_context import set_authenticated
from server.tests.slice4 import conftest as space_fixtures
from sqlalchemy import select, text
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.db.metadata import asset_identifiers, asset_initial_facts, asset_transitions, assets
from fleetops.db.tenancy import apply_tenant_policy

space_data = space_fixtures.space_data
space_password_hash = space_fixtures.space_password_hash

TABLES = (assets, asset_identifiers, asset_transitions)
FUNCTION_SIGNATURE = (
    "fleetops.transition_asset(uuid, integer, text, text, text, timestamptz, uuid, uuid, uuid)"
)


def call_transition(connection, tenant, **changes):
    "Invoke the real privileged boundary from a caller-selected runtime transaction."
    params = (
        dict(
            asset_id=tenant.asset_id,
            expected_version=1,
            from_state="RECEIVED",
            to_state="IN_STOCK",
            reason="Placed",
            occurred_at=datetime.now(UTC),
            evidence_ref=None,
            client_op_id=None,
            transition_id=uuid7(),
        )
        | changes
    )
    return dict(
        connection.execute(
            text("""
        SELECT * FROM fleetops.transition_asset(
            :asset_id, :expected_version, :from_state, :to_state, :reason,
            :occurred_at, :evidence_ref, :client_op_id, :transition_id)
    """),
            params,
        )
        .mappings()
        .one()
    )


@pytest.fixture
def seed_asset(space_data, migrator_connection):
    "Create version-1 history and ADR-007 facts atomically from explicit fixture inputs."

    def seed(tenant, *, history=True, initial_facts=True, **changes):
        values = dict(
            id=uuid7(),
            org_id=tenant.org_id,
            item_id=tenant.item_id,
            asset_tag=str(uuid7()),
            description="Fixture workstation",
            owner_party_id=tenant.vendor_id,
            custodian_party_id=tenant.party_id,
            current_location_id=tenant.location_id,
            current_state="RECEIVED",
            version=1,
            created_by_actor_id=tenant.actor_id,
            updated_by_actor_id=tenant.actor_id,
        )
        values |= changes
        row = dict(
            migrator_connection.execute(assets.insert().values(values).returning(assets))
            .mappings()
            .one()
        )
        if history:
            migrator_connection.execute(
                asset_transitions.insert().values(
                    id=uuid7(),
                    org_id=tenant.org_id,
                    asset_id=row["id"],
                    result_version=1,
                    from_state=None,
                    to_state="RECEIVED",
                    reason="Controlled receiving fixture",
                    actor_id=tenant.actor_id,
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        if initial_facts:
            # Both records share declared creation inputs. This is not a backfill from
            # a stored projection; missing-baseline proofs explicitly opt out.
            migrator_connection.execute(
                asset_initial_facts.insert().values(
                    asset_id=values["id"],
                    org_id=values["org_id"],
                    initial_owner_party_id=values["owner_party_id"],
                    initial_custodian_party_id=values["custodian_party_id"],
                    initial_location_id=values["current_location_id"],
                    actor_id=values["created_by_actor_id"],
                    occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
                )
            )
        migrator_connection.commit()
        return row

    return seed


@pytest.fixture
def asset_data(space_data, seed_asset, migrator_connection):
    "A/B fixtures include location, owner, custodian, readable identifier and initial history."
    for tenant in space_data:
        row = seed_asset(tenant, asset_tag="FLEET-001")
        tenant.asset_id = row["id"]
        tenant.identifier_id = uuid7()
        migrator_connection.execute(
            asset_identifiers.insert().values(
                id=tenant.identifier_id,
                org_id=tenant.org_id,
                asset_id=tenant.asset_id,
                type="MANUFACTURER_SERIAL",
                value="SERIAL-001",
                created_by_actor_id=tenant.actor_id,
            )
        )
    migrator_connection.commit()
    return space_data


@pytest.fixture
def asset_client(database, asset_data):
    "Use actual bearer resolution, request transactions, and RLS at the API boundary."
    with TestClient(create_app(database.settings(asset_data[0].org_id))) as client:
        yield client


@pytest.fixture
def snapshot(migrator_connection):
    "Inspect complete committed state/history independently of the runtime operation."

    def read(tenant):
        result = (
            dict(
                migrator_connection.execute(select(assets).where(assets.c.id == tenant.asset_id))
                .mappings()
                .one()
            ),
            [
                dict(row)
                for row in migrator_connection.execute(
                    select(asset_transitions)
                    .where(asset_transitions.c.asset_id == tenant.asset_id)
                    .order_by(asset_transitions.c.result_version)
                ).mappings()
            ],
        )
        migrator_connection.rollback()
        return result

    return read


@pytest.fixture
def asset_dml(asset_data, migrator_connection, app_connection):
    "Separate RLS from narrower production grants, restoring those exact grants afterward."
    for table in TABLES:
        migrator_connection.exec_driver_sql(
            f"GRANT INSERT, UPDATE, DELETE ON fleetops.{table.name} TO fleetops_app"
        )
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for table in TABLES:
            apply_tenant_policy(migrator_connection, table, privileges=("SELECT",))
        migrator_connection.exec_driver_sql(
            "GRANT UPDATE (asset_tag, description, updated_by_actor_id, updated_at) "
            "ON fleetops.assets TO fleetops_app"
        )
        migrator_connection.commit()


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def runtime_transition(connection, tenant, **changes):
    """Commit one real runtime transition; each call owns a separate transaction."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return call_transition(connection, tenant, **changes)
