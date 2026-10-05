"""Server choices preserve tenant isolation, history authority and global-version claims."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from server.tests.slice5.conftest import headers
from sqlalchemy import select

from fleetops.db.metadata import assets


def options(client, tenant, asset_id=None):
    return client.get(
        f"/assets/{asset_id or tenant.asset_id}/transition-options",
        headers=headers(tenant),
    )


def transition(client, tenant, version, source, target):
    return client.post(
        f"/assets/{tenant.asset_id}/transitions",
        headers=headers(tenant),
        json={
            "expected_version": version,
            "from_state": source,
            "to_state": target,
            "reason": "State choices proof",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )


def test_choices_require_bearer_and_hide_missing_or_other_tenant(asset_client, asset_data):
    a, b = asset_data
    assert asset_client.get(f"/assets/{a.asset_id}/transition-options").status_code == 401
    assert options(asset_client, a, b.asset_id).status_code == 404
    assert options(asset_client, a, uuid4()).status_code == 404
    response = options(asset_client, a)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "asset_id": str(a.asset_id),
        "expected_version": 1,
        "from_state": "RECEIVED",
        "options": [
            {"to_state": "IN_STOCK", "requires_evidence": False},
            {"to_state": "ON_HOLD", "requires_evidence": False},
        ],
    }


def test_dynamic_hold_exit_and_retirement_requirement_come_from_server(asset_client, asset_data):
    a, _ = asset_data
    assert transition(asset_client, a, 1, "RECEIVED", "IN_STOCK").status_code == 201
    assert transition(asset_client, a, 2, "IN_STOCK", "ON_HOLD").status_code == 201
    value = options(asset_client, a).json()
    assert value["expected_version"] == 3
    assert value["from_state"] == "ON_HOLD"
    assert value["options"] == [
        {"to_state": "IN_STOCK", "requires_evidence": False},
        {"to_state": "OUT_OF_SERVICE", "requires_evidence": False},
    ]
    assert transition(asset_client, a, 3, "ON_HOLD", "IN_STOCK").status_code == 201
    assert {"to_state": "RETIRED", "requires_evidence": True} in options(asset_client, a).json()[
        "options"
    ]
    assert transition(asset_client, a, 4, "IN_STOCK", "RETIRED").status_code == 422


def test_movement_advances_option_version_without_inventing_state_history(asset_client, asset_data):
    a, _ = asset_data
    before = options(asset_client, a).json()
    moved = asset_client.post(
        f"/assets/{a.asset_id}/movements",
        headers=headers(a),
        json={
            "expected_version": 1,
            "to_location_id": None,
            "reason": "At dock",
            "occurred_at": datetime.now(UTC).isoformat(),
        },
    )
    assert moved.status_code == 201
    after = options(asset_client, a).json()
    assert after["expected_version"] == 2
    assert after["options"] == before["options"]
    assert after["from_state"] == before["from_state"]
    # A previously offered choice is not a reservation.
    assert transition(asset_client, a, 1, "RECEIVED", "IN_STOCK").status_code == 409


@pytest.mark.parametrize("missing", [True, False])
def test_corrupt_projection_or_missing_history_offers_no_choices(
    missing,
    asset_client,
    asset_data,
    seed_asset,
    migrator_connection,
):
    a, _ = asset_data
    if missing:
        target = seed_asset(a, history=False)["id"]
    else:
        target = a.asset_id
        migrator_connection.execute(
            assets.update().where(assets.c.id == target).values(current_state="IN_STOCK")
        )
        migrator_connection.commit()
    before = (
        migrator_connection.execute(select(assets).where(assets.c.id == target)).mappings().one()
    )
    migrator_connection.rollback()
    assert options(asset_client, a, target).status_code == 409
    after = (
        migrator_connection.execute(select(assets).where(assets.c.id == target)).mappings().one()
    )
    assert dict(before) == dict(after)
    migrator_connection.rollback()
