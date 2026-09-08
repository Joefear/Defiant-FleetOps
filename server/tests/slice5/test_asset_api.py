"Authenticated Asset workflows, strict authority boundaries, and Python lifecycle legality."

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from server.tests.slice5.conftest import headers
from uuid6 import uuid7

from fleetops.db.metadata import actors, assets
from fleetops.domain.assets import transition_asset
from fleetops.domain.lifecycle import LIFECYCLE, LifecycleInvalid, validate_edge
from fleetops.domain.lifecycle import AssetState as S

PATHS = {
    "RECEIVED": [],
    "IN_STOCK": ["IN_STOCK"],
    "CONFIGURING": ["IN_STOCK", "CONFIGURING"],
    "READY": ["IN_STOCK", "CONFIGURING", "READY"],
    "DEPLOYED": ["IN_STOCK", "CONFIGURING", "READY", "DEPLOYED"],
    "OUT_OF_SERVICE": ["IN_STOCK", "CONFIGURING", "READY", "DEPLOYED", "OUT_OF_SERVICE"],
}
NORMAL_EDGES = [
    (source.value, target.value)
    for source, targets in LIFECYCLE.items()
    if source not in {S.ON_HOLD, S.RETIRED}
    for target in sorted(targets)
    if target != S.RETIRED
]


def post(client, tenant, source, target, expected, **changes):
    body = (
        dict(
            expected_version=expected,
            from_state=source,
            to_state=target,
            reason="Lifecycle proof",
            occurred_at="2026-06-01T12:00:00+00:00",
        )
        | changes
    )
    return client.post(f"/assets/{tenant.asset_id}/transitions", json=body, headers=headers(tenant))


def walk(client, tenant, state):
    "Reach each test state using real legal production transitions from the initial pair."
    current, version = "RECEIVED", 1
    for target in PATHS[state]:
        response = post(client, tenant, current, target, version)
        assert response.status_code == 201, response.text
        current, version = target, version + 1
    return version


@pytest.mark.parametrize("source,target", NORMAL_EDGES)
def test_every_executable_normal_graph_edge(source, target, asset_client, asset_data, snapshot):
    a, _ = asset_data
    version = walk(asset_client, a, source)
    before, _ = snapshot(a)
    response = post(asset_client, a, source, target, version)
    assert response.status_code == 201, response.text
    after, history = snapshot(a)
    assert after["version"] == history[-1]["result_version"] == version + 1
    assert after["current_state"] == target
    for column in (
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "current_assignment_id",
    ):
        assert before[column] == after[column]


@pytest.mark.parametrize(
    "entry,exit_state,status",
    [
        ("READY", "READY", 201),
        ("READY", "OUT_OF_SERVICE", 201),
        ("READY", "IN_STOCK", 422),
        ("RECEIVED", "RECEIVED", 201),
    ],
)
def test_on_hold_uses_authoritative_entry_state(
    entry, exit_state, status, asset_client, asset_data, snapshot
):
    a, _ = asset_data
    version = walk(asset_client, a, entry)
    before, _ = snapshot(a)
    assert post(asset_client, a, entry, "ON_HOLD", version).status_code == 201
    held = snapshot(a)
    response = post(asset_client, a, "ON_HOLD", exit_state, version + 1)
    assert response.status_code == status, response.text
    after, _ = snapshot(a)
    for column in (
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "current_assignment_id",
    ):
        assert before[column] == held[0][column] == after[column]
    if status == 422:
        assert snapshot(a) == held


def test_on_hold_history_lookup_ignores_global_version_gaps(
    asset_client, asset_data, migrator_connection
):
    a, _ = asset_data
    walk(asset_client, a, "READY")
    migrator_connection.execute(assets.update().where(assets.c.id == a.asset_id).values(version=6))
    migrator_connection.commit()
    assert post(asset_client, a, "READY", "ON_HOLD", 6).status_code == 201
    assert post(asset_client, a, "ON_HOLD", "READY", 7).status_code == 201


@pytest.mark.parametrize(
    "source,target",
    [
        ("READY", "RETIRED"),
        ("RECEIVED", "DEPLOYED"),
        ("IN_STOCK", "RECEIVED"),
        ("RETIRED", "READY"),
    ],
)
def test_illegal_edge_rejects_in_python_before_any_database_call(source, target):
    connection = Mock()
    with pytest.raises(LifecycleInvalid):
        transition_asset(
            connection,
            uuid7(),
            values=dict(
                from_state=S(source),
                to_state=S(target),
                expected_version=1,
                reason="Invalid",
                occurred_at=datetime.now(UTC),
            ),
        )
    connection.execute.assert_not_called()


@pytest.mark.parametrize("source", [S.IN_STOCK, S.OUT_OF_SERVICE])
def test_legal_retirement_edges_remain_fail_closed(source, asset_client, asset_data, snapshot):
    validate_edge(source, S.RETIRED)
    a, _ = asset_data
    version = walk(asset_client, a, source)
    before = snapshot(a)
    response = post(asset_client, a, source, "RETIRED", version)
    assert response.status_code == 422
    assert "evidence" in response.json()["detail"]
    assert snapshot(a) == before


def test_canonical_graph_and_retired_terminal():
    assert {s.value for s in S} == {
        "RECEIVED",
        "IN_STOCK",
        "CONFIGURING",
        "READY",
        "DEPLOYED",
        "ON_HOLD",
        "OUT_OF_SERVICE",
        "RETIRED",
    }
    assert {s.value: {t.value for t in targets} for s, targets in LIFECYCLE.items()} == {
        "RECEIVED": {"IN_STOCK", "ON_HOLD"},
        "IN_STOCK": {"CONFIGURING", "ON_HOLD", "RETIRED"},
        "CONFIGURING": {"READY", "ON_HOLD"},
        "READY": {"DEPLOYED", "IN_STOCK", "ON_HOLD"},
        "DEPLOYED": {"ON_HOLD", "OUT_OF_SERVICE", "READY"},
        "ON_HOLD": {"OUT_OF_SERVICE"},
        "OUT_OF_SERVICE": {"CONFIGURING", "RETIRED"},
        "RETIRED": set(),
    }
    for state in S:
        with pytest.raises(LifecycleInvalid):
            validate_edge(S.RETIRED, state)


ROUTES = [
    ("GET", "/assets"),
    ("GET", "/assets/{id}"),
    ("PATCH", "/assets/{id}"),
    ("GET", "/assets/{id}/identifiers"),
    ("GET", "/assets/{id}/transitions"),
    ("POST", "/assets/{id}/transitions"),
    ("GET", "/health/assets/reconciliation"),
]


@pytest.mark.parametrize("method,path", ROUTES)
def test_every_asset_route_requires_authentication(method, path, asset_client, asset_data):
    response = asset_client.request(method, path.format(id=asset_data[0].asset_id), json={})
    assert response.status_code == 401
    response = asset_client.request(
        method,
        path.format(id=asset_data[0].asset_id),
        json={},
        headers={"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401


def test_reads_patch_attribution_uuid_and_receiving_boundary(
    asset_client, asset_data, snapshot, migrator_connection
):
    a, b = asset_data
    assert [row["id"] for row in asset_client.get("/assets", headers=headers(a)).json()] == [
        str(a.asset_id)
    ]
    assert (
        asset_client.get(f"/assets/{a.asset_id}/identifiers", headers=headers(a)).json()[0]["value"]
        == "SERIAL-001"
    )
    before, history = snapshot(a)
    # Use a different authenticated actor to prove updater attribution changes.
    other_actor = uuid7()
    migrator_connection.execute(
        actors.insert().values(
            id=other_actor,
            org_id=a.org_id,
            type="HUMAN",
            display_name="Original creator",
            created_by_actor_id=a.actor_id,
        )
    )
    migrator_connection.execute(
        assets.update()
        .where(assets.c.id == a.asset_id)
        .values(updated_by_actor_id=other_actor, updated_at=datetime(2000, 1, 1, tzinfo=UTC))
    )
    migrator_connection.commit()
    response = asset_client.patch(
        f"/assets/{a.asset_id}",
        json={"asset_tag": "RENAMED", "description": "Edited"},
        headers=headers(a),
    )
    assert response.status_code == 200, response.text
    after, after_history = snapshot(a)
    assert after["id"] == before["id"]
    assert after["asset_tag"] == "RENAMED" and after["description"] == "Edited"
    assert after["updated_by_actor_id"] == a.actor_id
    assert after["updated_at"] > datetime(2000, 1, 1, tzinfo=UTC)
    assert after["created_at"] == before["created_at"]
    assert after["created_by_actor_id"] == before["created_by_actor_id"]
    assert after["version"] == before["version"] and after_history == history
    for method, path in [
        ("POST", "/assets"),
        ("DELETE", f"/assets/{a.asset_id}"),
        ("POST", f"/assets/{a.asset_id}/identifiers"),
        ("POST", "/asset-identifiers"),
    ]:
        assert asset_client.request(method, path, headers=headers(a), json={}).status_code in {
            404,
            405,
        }
    for path in (
        f"/assets/{b.asset_id}",
        f"/assets/{b.asset_id}/identifiers",
        f"/assets/{b.asset_id}/transitions",
    ):
        assert asset_client.get(path, headers=headers(a)).status_code == 404


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "org_id",
        "actor_id",
        "item_id",
        "created_by_actor_id",
        "updated_by_actor_id",
        "created_at",
        "updated_at",
        "current_state",
        "version",
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "current_assignment_id",
        "recorded_at",
        "result_version",
    ],
)
def test_patch_cannot_supply_authority(field, asset_client, asset_data, snapshot):
    a, _ = asset_data
    before = snapshot(a)
    assert (
        asset_client.patch(
            f"/assets/{a.asset_id}",
            json={"description": "Attempt", field: "spoof"},
            headers=headers(a),
        ).status_code
        == 422
    )
    assert snapshot(a) == before


@pytest.mark.parametrize(
    "field",
    [
        "org_id",
        "actor_id",
        "created_by_actor_id",
        "updated_by_actor_id",
        "recorded_at",
        "result_version",
        "evidence_ref",
        "corrects_transition_id",
        "client_op_id",
        "version",
    ],
)
def test_transition_cannot_supply_authority(field, asset_client, asset_data, snapshot):
    a, _ = asset_data
    before = snapshot(a)
    assert post(asset_client, a, "RECEIVED", "IN_STOCK", 1, **{field: "spoof"}).status_code == 422
    assert snapshot(a) == before


@pytest.mark.parametrize(
    "changes",
    [
        {"occurred_at": "2026-01-01T12:00:00"},
        {"expected_version": True},
        {"expected_version": 0},
        {"from_state": "ORDERED"},
    ],
)
def test_transition_rejects_invalid_claims(changes, asset_client, asset_data):
    assert (
        post(asset_client, asset_data[0], "RECEIVED", "IN_STOCK", 1, **changes).status_code == 422
    )


def test_history_order_and_reconciliation_ignore_occurrence_clocks(
    asset_client, asset_data, snapshot
):
    a, b = asset_data
    future = "2099-01-01T01:00:00+01:00"
    past = "1970-01-01T00:00:00+00:00"
    assert post(asset_client, a, "RECEIVED", "IN_STOCK", 1, occurred_at=future).status_code == 201
    assert post(asset_client, a, "IN_STOCK", "CONFIGURING", 2, occurred_at=past).status_code == 201
    history = asset_client.get(f"/assets/{a.asset_id}/transitions", headers=headers(a)).json()
    assert [row["result_version"] for row in history] == [1, 2, 3]
    assert datetime.fromisoformat(history[1]["occurred_at"]) == datetime.fromisoformat(future)
    assert datetime.fromisoformat(history[2]["occurred_at"]) == datetime.fromisoformat(past)
    assert all(datetime.fromisoformat(row["recorded_at"]).tzinfo is not None for row in history)
    assert all(row["actor_id"] == str(a.actor_id) for row in history)
    assert history[2]["recorded_at"] != history[2]["occurred_at"]
    assert asset_client.get("/health/assets/reconciliation", headers=headers(a)).json() == []
    assert asset_client.get("/health/assets/reconciliation", headers=headers(b)).json() == []
    before = snapshot(a)
    assert post(asset_client, a, "CONFIGURING", "READY", 2).status_code == 409
    assert snapshot(a) == before


def test_asset_api_tenant_authority_and_reconciliation_isolation(
    asset_client, asset_data, migrator_connection, snapshot
):
    a, b = asset_data
    before = snapshot(b)
    body = dict(
        expected_version=1,
        from_state="RECEIVED",
        to_state="IN_STOCK",
        reason="Cross-tenant attempt",
        occurred_at="2026-01-01T00:00:00Z",
    )
    responses = [
        asset_client.post(f"/assets/{target}/transitions", json=body, headers=headers(a))
        for target in (b.asset_id, uuid7())
    ]
    assert all(response.status_code == 404 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert snapshot(b) == before
    response = asset_client.get(
        "/assets",
        headers=headers(a) | {"X-Org-ID": str(b.org_id)},
        params={"org_id": str(b.org_id), "actor_id": str(b.actor_id)},
    )
    assert [row["id"] for row in response.json()] == [str(a.asset_id)]
    migrator_connection.execute(
        assets.update().where(assets.c.id == b.asset_id).values(current_state="READY")
    )
    migrator_connection.commit()
    assert asset_client.get("/health/assets/reconciliation", headers=headers(a)).json() == []
    discrepancies = asset_client.get("/health/assets/reconciliation", headers=headers(b)).json()
    assert len(discrepancies) == 1 and discrepancies[0]["asset_id"] == str(b.asset_id)


@pytest.mark.parametrize(
    "body", [{}, {"description": None}, {"asset_tag": None}, {"asset_tag": "  "}]
)
def test_patch_rejects_empty_or_null_edits(body, asset_client, asset_data, snapshot):
    a, _ = asset_data
    before = snapshot(a)
    assert (
        asset_client.patch(f"/assets/{a.asset_id}", json=body, headers=headers(a)).status_code
        == 422
    )
    assert snapshot(a) == before
