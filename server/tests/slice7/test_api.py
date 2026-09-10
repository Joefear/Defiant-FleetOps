"""Strict authenticated Slice 7 HTTP boundaries and immutable read contracts."""

import pytest
from server.tests.slice5.conftest import headers
from server.tests.slice6.conftest import for_asset
from server.tests.slice7.conftest import runtime_assignment
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import asset_initial_assignment_facts, assets

ROUTES = (
    ("POST", "assignments"),
    ("POST", "unassignment"),
    ("GET", "assignments"),
    ("POST", "configurations"),
    ("GET", "configurations"),
    ("GET", "configurations/current"),
)


def body(tenant, route, **changes):
    if route.startswith("configurations"):
        values = dict(
            image_name="Fleet image",
            image_version="1.0",
            config_profile="station",
            applied_at="2026-05-01T10:00:00-05:00",
        )
    else:
        values = dict(
            expected_version=1,
            reason="Assigned for testing",
            occurred_at="2026-05-01T10:00:00-05:00",
        )
        if route == "assignments":
            values |= dict(assignee_type="LOCATION", assignee_id=str(tenant.location_id))
    return values | changes


@pytest.mark.parametrize("method,route", ROUTES)
@pytest.mark.parametrize("credential", [None, "unissued-credential"])
def test_all_routes_require_authentication(
    method, route, credential, asset_client, asset_data, assignment_snapshot
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    response = asset_client.request(
        method,
        f"/assets/{a.asset_id}/{route}",
        json=body(a, route),
        headers={} if credential is None else {"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 401
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("route", ["assignments", "unassignment", "configurations"])
def test_strict_requests_reject_every_authority_field(
    route, asset_client, asset_data, other_human, assignment_snapshot
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    forbidden = {
        "id",
        "asset_id",
        "org_id",
        "actor_id",
        "applied_by",
        "created_by_actor_id",
        "updated_by_actor_id",
        "recorded_at",
        "result_version",
        "version",
        "assets.version",
        "current_assignment_id",
        "current_state",
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "from_assignee_type",
        "from_assignee_id",
        "to_assignee_type",
        "to_assignee_id",
        "corrects_assignment_event_id",
        "client_op_id",
        "configuration_seq",
        "evidence_ref",
        "initial_assignment_fact",
        "unexpected_authority",
    }
    if route == "unassignment":
        forbidden |= {"assignee_type", "assignee_id"}
    if route == "configurations":
        forbidden |= {"expected_version", "assignee_type", "assignee_id"}
    for field in sorted(forbidden):
        response = asset_client.post(
            f"/assets/{a.asset_id}/{route}",
            headers=headers(a),
            json=body(a, route, **{field: str(other_human.actor_id)}),
        )
        assert response.status_code == 422, (field, response.text)
        assert any(e["type"] == "extra_forbidden" for e in response.json()["detail"])
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("route", ["assignments", "unassignment", "configurations"])
def test_required_business_inputs_are_bounded_and_times_are_aware(
    route, asset_client, asset_data, assignment_snapshot
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    if route == "configurations":
        invalid = [
            {field: value}
            for field in ("image_name", "image_version", "config_profile")
            for value in (None, "", " \t\n", "x" * 201)
        ] + [{"notes": value} for value in (None, "x" * 4001)]
        time_field = "applied_at"
    else:
        invalid = [
            {"expected_version": value} for value in (None, True, "1", 1.0, 0, -1, 2147483648)
        ] + [{"reason": value} for value in (None, "", " \t\n", "x" * 4001)]
        if route == "assignments":
            invalid += [{"assignee_type": v} for v in (None, "USER", "actor")]
            invalid += [{"assignee_id": v} for v in (None, "not-a-uuid")]
        time_field = "occurred_at"
    invalid += [{time_field: v} for v in (None, "2026-01-01T12:00:00", "not-a-date")]
    payloads = [body(a, route, **changes) for changes in invalid]
    for field in body(a, route):
        payloads.append({k: v for k, v in body(a, route).items() if k != field})
    for payload in payloads:
        response = asset_client.post(
            f"/assets/{a.asset_id}/{route}",
            headers=headers(a),
            json=payload,
        )
        assert response.status_code == 422, response.text
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("method,route", ROUTES)
def test_cross_tenant_and_missing_asset_fail_identically_without_writes(
    method, route, asset_client, asset_data, assignment_snapshot
):
    a, b = asset_data
    before = assignment_snapshot(a), assignment_snapshot(b)
    responses = [
        asset_client.request(
            method,
            f"/assets/{target}/{route}",
            headers=headers(a),
            json=body(a, route),
        )
        for target in (b.asset_id, uuid7())
    ]
    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].json() == responses[1].json() == {"detail": "Asset not found"}
    assert (assignment_snapshot(a), assignment_snapshot(b)) == before


@pytest.mark.parametrize(
    "kind,attribute",
    [
        ("ACTOR", "actor_id"),
        ("LOCATION", "location_id"),
        ("PARTY", "party_id"),
    ],
)
def test_api_rejects_cross_tenant_missing_and_wrong_type_targets(
    kind, attribute, asset_client, asset_data, assignment_snapshot
):
    a, b = asset_data
    before = assignment_snapshot(a), assignment_snapshot(b)
    wrong = a.party_id if kind != "PARTY" else a.actor_id
    for target in (getattr(b, attribute), uuid7(), wrong):
        response = asset_client.post(
            f"/assets/{a.asset_id}/assignments",
            headers=headers(a),
            json=body(a, "assignments", assignee_type=kind, assignee_id=str(target)),
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid Asset operation or attribution"}
    assert (assignment_snapshot(a), assignment_snapshot(b)) == before


def test_assignment_api_events_intervals_projection_and_actor_authority(
    asset_client, asset_data, other_human, assignment_snapshot
):
    a, b = asset_data
    path = f"/assets/{a.asset_id}"
    spoof = headers(a) | {"X-Org-ID": str(b.org_id), "X-Actor-ID": str(other_human.actor_id)}
    events = []
    for index, (route, changes) in enumerate(
        [
            ("assignments", dict(assignee_type="ACTOR", assignee_id=str(other_human.actor_id))),
            ("assignments", {}),
            ("unassignment", {}),
        ]
    ):
        response = asset_client.post(
            f"{path}/{route}",
            headers=spoof,
            params={"org_id": str(b.org_id), "actor_id": str(other_human.actor_id)},
            json=body(a, route, expected_version=index + 1, **changes),
        )
        assert response.status_code == 201, response.text
        event = response.json()
        assert event["event_type"] == ["ASSIGN", "REASSIGN", "UNASSIGN"][index]
        assert event["actor_id"] == str(a.actor_id) and event["org_id"] == str(a.org_id)
        assert event["result_version"] == index + 2
        assert event["corrects_assignment_event_id"] is None and event["client_op_id"] is None
        events.append(event)
        asset = asset_client.get(path, headers=headers(a)).json()
        assert asset["version"] == index + 2
        assert asset["current_assignment_id"] == (event["id"] if index < 2 else None)
        before = assignment_snapshot(a)
        stale = asset_client.post(
            f"{path}/{route}",
            headers=headers(a),
            json=body(a, route, **changes),
        )
        assert stale.status_code == 409 and assignment_snapshot(a) == before
    history = asset_client.get(f"{path}/assignments", headers=headers(a)).json()
    assert history["initial_assignment_fact"]["actor_id"] == str(a.actor_id)
    assert history["events"] == events
    assert [r["establishing_event_id"] for r in history["intervals"]] == [
        e["id"] for e in events[:2]
    ]
    assert [r["ended_at"] for r in history["intervals"]] == [e["occurred_at"] for e in events[1:]]


@pytest.mark.parametrize("corruption", ["missing-witness", "projection"])
def test_inconsistent_assignment_returns_409_without_repair(
    corruption,
    asset_client,
    asset_data,
    seed_asset,
    migrator_connection,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    if corruption == "missing-witness":
        a = for_asset(a, seed_asset(a, initial_assignment=False)["id"])
    else:
        runtime_assignment(app_connection, a)
        migrator_connection.execute(
            assets.update().where(assets.c.id == a.asset_id).values(current_assignment_id=None)
        )
        migrator_connection.commit()
    before = assignment_snapshot(a)
    for route in ("assignments", "unassignment"):
        response = asset_client.post(
            f"/assets/{a.asset_id}/{route}",
            headers=headers(a),
            json=body(a, route, expected_version=before["asset"]["version"]),
        )
        assert response.status_code == 409
    health = asset_client.get("/health/assets/reconciliation", headers=headers(a)).json()
    row = next(r for r in health if r["asset_id"] == str(a.asset_id))
    expected = (
        "missing_initial_assignment_facts"
        if corruption == "missing-witness"
        else ("assignment_projection_mismatch")
    )
    assert expected in row["discrepancies"]
    assert assignment_snapshot(a) == before


def test_configuration_api_uses_credential_defaults_and_hides_global_allocation(
    asset_client, asset_data, other_human, assignment_snapshot
):
    a, b = asset_data
    before = assignment_snapshot(a)
    path = f"/assets/{a.asset_id}/configurations"
    returned = []
    for year in (2099, 1970):
        response = asset_client.post(
            path,
            headers=headers(a) | {"X-Actor-ID": str(other_human.actor_id)},
            params={"org_id": str(b.org_id), "actor_id": str(other_human.actor_id)},
            json=body(a, "configurations", applied_at=f"{year}-01-01T00:00:00Z"),
        )
        assert response.status_code == 201, response.text
        row = response.json()
        assert row["org_id"] == str(a.org_id) and row["applied_by"] == str(a.actor_id)
        assert row["evidence_ref"] is None and row["notes"] == ""
        assert "configuration_seq" not in row and "result_version" not in row
        assert row["recorded_at"] != row["applied_at"]
        returned.append(row)
    assert asset_client.get(path, headers=headers(a)).json() == returned
    assert asset_client.get(f"{path}/current", headers=headers(a)).json() == returned[-1]
    after = assignment_snapshot(a)
    assert after | {"asset_configurations": before["asset_configurations"]} == before
    schema = asset_client.get("/openapi.json").json()["components"]["schemas"]
    assert "configuration_seq" not in schema["ConfigurationRequest"]["properties"]
    assert "configuration_seq" not in schema["ConfigurationOut"]["properties"]


def test_no_asset_witness_correction_or_mutable_history_routes(
    asset_client, asset_data, assignment_snapshot, migrator_connection
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    count = len(migrator_connection.execute(select(asset_initial_assignment_facts)).all())
    migrator_connection.rollback()
    paths = [
        ("POST", path)
        for path in (
            "/assets",
            "/asset-initial-assignment-facts",
            f"/assets/{a.asset_id}/initial-assignment-facts",
        )
    ]
    for route in ("assignments", "configurations"):
        paths += [
            (method, f"/assets/{a.asset_id}/{route}/{uuid7()}")
            for method in ("PATCH", "PUT", "DELETE", "POST")
        ]
        paths += [
            (method, f"/assets/{a.asset_id}/{route}") for method in ("PATCH", "PUT", "DELETE")
        ]
    for method, path in paths:
        assert asset_client.request(method, path, headers=headers(a), json={}).status_code in {
            404,
            405,
        }
    assert assignment_snapshot(a) == before
    assert len(migrator_connection.execute(select(asset_initial_assignment_facts)).all()) == count
