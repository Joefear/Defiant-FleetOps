"""Authenticated HTTP boundaries reject caller authority and expose scoped immutable facts."""

import pytest
from server.tests.slice5.conftest import headers
from server.tests.slice6.conftest import KINDS, MOVE, OWNERSHIP, for_asset
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import asset_initial_facts, asset_transitions, assets


def body(tenant, kind, **changes):
    return {
        "expected_version": 1,
        kind.to_field: str(getattr(tenant, kind.target_attribute)),
        "reason": "Reported physical change",
        "occurred_at": "2026-05-01T10:00:00-05:00",
    } | changes


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("credential", [None, "unissued-credential"])
def test_all_six_routes_require_bearer(
    kind,
    method,
    credential,
    asset_client,
    asset_data,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    response = asset_client.request(
        method,
        f"/assets/{a.asset_id}/{kind.route}",
        json=body(a, kind),
        headers={} if credential is None else {"Authorization": f"Bearer {credential}"},
    )
    assert response.status_code == 401
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_strict_body_never_accepts_actor_or_fact_authority(
    kind,
    asset_client,
    asset_data,
    other_human,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    forbidden = [
        "id",
        "asset_id",
        "org_id",
        "actor_id",
        "created_by_actor_id",
        "updated_by_actor_id",
        "from_location_id",
        "from_custodian_party_id",
        "from_owner_party_id",
        "result_version",
        "recorded_at",
        "version",
        "current_state",
        "current_assignment_id",
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "initial_owner_party_id",
        "initial_custodian_party_id",
        "initial_location_id",
        "asset_initial_facts",
        "client_op_id",
        "corrects_movement_id",
        "corrects_custody_change_id",
        "corrects_ownership_change_id",
    ]
    for field in forbidden:
        response = asset_client.post(
            f"/assets/{a.asset_id}/{kind.route}",
            headers=headers(a),
            json=body(a, kind, **{field: str(other_human.actor_id)}),
        )
        assert response.status_code == 422, (field, response.text)
        assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_required_claims_are_strict_and_timezone_aware(
    kind,
    asset_client,
    asset_data,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    invalid = (
        [{"expected_version": value} for value in (None, True, "1", 1.0, 0, -1, 2147483648)]
        + [{"reason": value} for value in (None, "", " \t\n", "x" * 4001)]
        + [{"occurred_at": value} for value in (None, "2026-01-01T12:00:00", "not-a-date")]
        + [{kind.to_field: "not-a-uuid"}]
    )
    if kind is OWNERSHIP:
        invalid.append({kind.to_field: None})
    payloads = [body(a, kind, **changes) for changes in invalid]
    for field in body(a, kind):
        payloads.append({key: value for key, value in body(a, kind).items() if key != field})
    for payload in payloads:
        response = asset_client.post(
            f"/assets/{a.asset_id}/{kind.route}",
            headers=headers(a),
            json=payload,
        )
        assert response.status_code == 422, response.text
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_api_target_isolation_stale_conflict_and_trusted_attribution(
    kind,
    asset_client,
    asset_data,
    other_human,
    fact_snapshot,
):
    a, b = asset_data
    before = fact_snapshot(a), fact_snapshot(b)
    for method in ("GET", "POST"):
        responses = [
            asset_client.request(
                method,
                f"/assets/{target}/{kind.route}",
                headers=headers(a),
                json=body(a, kind),
            )
            for target in (b.asset_id, uuid7())
        ]
        assert [response.status_code for response in responses] == [404, 404]
        assert responses[0].json() == responses[1].json() == {"detail": "Asset not found"}
    for target in (b.location_id if kind is MOVE else b.party_id, uuid7()):
        response = asset_client.post(
            f"/assets/{a.asset_id}/{kind.route}",
            headers=headers(a),
            json=body(a, kind, **{kind.to_field: str(target)}),
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid Asset operation or attribution"}
    assert (fact_snapshot(a), fact_snapshot(b)) == before
    response = asset_client.post(
        f"/assets/{a.asset_id}/{kind.route}",
        json=body(a, kind),
        headers=headers(a) | {"X-Org-ID": str(b.org_id), "X-Actor-ID": str(other_human.actor_id)},
        params={"org_id": str(b.org_id), "actor_id": str(other_human.actor_id)},
    )
    assert response.status_code == 201, response.text
    assert response.json()["actor_id"] == str(a.actor_id)
    assert response.json()["org_id"] == str(a.org_id)
    after = fact_snapshot(a)
    assert (
        asset_client.post(
            f"/assets/{a.asset_id}/{kind.route}",
            headers=headers(a),
            json=body(a, kind),
        ).status_code
        == 409
    )
    assert fact_snapshot(a) == after and fact_snapshot(b) == before[1]


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("corruption", ["missing-baseline", "first-projection", "later-projection"])
def test_api_history_conflicts_return_409_without_repair(
    kind,
    corruption,
    asset_client,
    asset_data,
    seed_asset,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    if corruption == "missing-baseline":
        a = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    elif corruption == "later-projection":
        assert (
            asset_client.post(
                f"/assets/{a.asset_id}/{kind.route}",
                headers=headers(a),
                json=body(a, kind),
            ).status_code
            == 201
        )
        # Restore the creation projection while retaining the legitimate first history event.
        initial = fact_snapshot(a)["initial"][kind.initial]
        migrator_connection.execute(
            assets.update().where(assets.c.id == a.asset_id).values(**{kind.projection: initial})
        )
        migrator_connection.commit()
    else:
        migrator_connection.execute(
            assets.update()
            .where(assets.c.id == a.asset_id)
            .values(**{kind.projection: getattr(a, kind.target_attribute)})
        )
        migrator_connection.commit()
    before = fact_snapshot(a)
    response = asset_client.post(
        f"/assets/{a.asset_id}/{kind.route}",
        headers=headers(a),
        json=body(a, kind, expected_version=before["asset"]["version"]),
    )
    assert response.status_code == 409
    assert fact_snapshot(a) == before


def test_extended_health_remains_tenant_scoped_and_never_repairs(
    asset_client,
    asset_data,
    migrator_connection,
    fact_snapshot,
):
    a, b = asset_data
    migrator_connection.execute(
        assets.update()
        .where(assets.c.id == b.asset_id)
        .values(
            current_location_id=b.other_location_id,
            custodian_party_id=None,
            owner_party_id=b.party_id,
            version=3,
        )
    )
    migrator_connection.commit()
    before = fact_snapshot(a), fact_snapshot(b)
    assert (
        asset_client.get(
            "/health/assets/reconciliation",
            headers=headers(a) | {"X-Org-ID": str(b.org_id)},
            params={"org_id": str(b.org_id)},
        ).json()
        == []
    )
    response = asset_client.get("/health/assets/reconciliation", headers=headers(b))
    assert response.status_code == 200
    assert len(response.json()) == 1
    row = response.json()[0]
    assert row["asset_id"] == str(b.asset_id) and row["initial_facts_present"] is True
    assert row["global_missing_version_ranges"] == [[2, 3]]
    assert set(row["discrepancies"]) == {
        "initial_location_projection_mismatch",
        "initial_custody_projection_mismatch",
        "initial_ownership_projection_mismatch",
        "global_version_missing",
    }
    assert (fact_snapshot(a), fact_snapshot(b)) == before


@pytest.mark.parametrize(
    "corruption,category",
    [
        ("missing", "missing_history"),
        ("state", "state_mismatch"),
        ("ahead", "history_version_ahead"),
    ],
)
def test_extended_health_preserves_slice5_state_discrepancies(
    corruption,
    category,
    asset_client,
    asset_data,
    migrator_connection,
):
    a = asset_data[0]
    if corruption == "missing":
        migrator_connection.execute(
            asset_transitions.delete().where(asset_transitions.c.asset_id == a.asset_id)
        )
    elif corruption == "state":
        migrator_connection.execute(
            assets.update().where(assets.c.id == a.asset_id).values(current_state="READY")
        )
    else:
        # Preserve the valid initial event while adding an impossible later version.
        initial = dict(
            migrator_connection.execute(
                select(asset_transitions).where(asset_transitions.c.asset_id == a.asset_id)
            )
            .mappings()
            .one()
        )
        migrator_connection.execute(
            asset_transitions.insert().values(
                initial | {"id": uuid7(), "result_version": 2, "from_state": "RECEIVED"}
            )
        )
    migrator_connection.commit()
    response = asset_client.get("/health/assets/reconciliation", headers=headers(a))
    assert response.status_code == 200
    row = response.json()[0]
    assert row["asset_id"] == str(a.asset_id) and category in row["discrepancies"]
    assert row["latest_result_version"] == {"missing": None, "state": 1, "ahead": 2}[corruption]


def test_no_creation_baseline_correction_or_mutable_history_routes(
    asset_client,
    asset_data,
    fact_snapshot,
    migrator_connection,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    count = len(migrator_connection.execute(select(asset_initial_facts)).all())
    migrator_connection.rollback()
    paths = [
        ("POST", "/assets"),
        ("POST", "/asset-initial-facts"),
        ("POST", f"/assets/{a.asset_id}/initial-facts"),
    ]
    for kind in KINDS:
        paths.extend(
            (method, f"/assets/{a.asset_id}/{kind.route}/{uuid7()}")
            for method in ("PATCH", "PUT", "DELETE", "POST")
        )
    for method, path in paths:
        assert asset_client.request(method, path, headers=headers(a), json={}).status_code in {
            404,
            405,
        }
    assert fact_snapshot(a) == before
    assert len(migrator_connection.execute(select(asset_initial_facts)).all()) == count
