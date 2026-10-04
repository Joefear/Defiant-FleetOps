"""Real authenticated tenants with independent per-operation transactions."""

from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from server.tests.slice5.conftest import asset_data, seed_asset, space_data, space_password_hash
from server.tests.slice9 import conftest as receiving_fixtures

from fleetops.api.app import create_app


@pytest.fixture
def receiving_cleanup(space_data, migrator_connection, app_connection):
    yield from receiving_fixtures.receiving_cleanup.__wrapped__(
        space_data, migrator_connection, app_connection
    )


__all__ = ["asset_data", "seed_asset", "space_data", "space_password_hash"]


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def operation(tenant, **changes):
    return {
        "operation_id": str(uuid4()),
        "actor_id": str(tenant.actor_id),
        "client_id": str(uuid4()),
        "client_epoch": str(uuid4()),
        "client_seq": 1,
        "entity_type": "ASSET",
        "entity_id": str(tenant.asset_id),
        "expected_version": 1,
        "operation": "MOVE",
        "payload": {"to_location_id": None, "reason": "Queued placement"},
        "occurred_at": "1970-01-01T00:00:00Z",
        **changes,
    }


def submit(client, tenant, operations):
    response = client.post(
        "/capture/operations", headers=headers(tenant), json={"operations": operations}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def capture_client(database, asset_data, tmp_path):
    settings = replace(database.settings(asset_data[0].org_id), evidence_root=tmp_path / "evidence")
    with TestClient(create_app(settings)) as client:
        yield client
