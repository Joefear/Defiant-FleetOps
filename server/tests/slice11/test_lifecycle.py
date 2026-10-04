"""Evidence opens only the retained legal retirement edges under the global Asset version."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import call_transition
from server.tests.slice11.conftest import WHEN, headers, link, transition, upload
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7


@pytest.mark.parametrize(
    "states",
    [
        ["RECEIVED", "IN_STOCK", "RETIRED"],
        ["RECEIVED", "IN_STOCK", "CONFIGURING", "READY", "DEPLOYED", "OUT_OF_SERVICE", "RETIRED"],
    ],
)
def test_legal_retirement_preserves_other_facts_and_consumes_one_version(
    states, evidence_client, asset_data
):
    tenant = asset_data[0]
    initial = evidence_client.get(f"/assets/{tenant.asset_id}", headers=headers(tenant)).json()
    row = upload(evidence_client, tenant).json()
    assert link(evidence_client, tenant, row["id"]).status_code == 201
    for version, (source, target) in enumerate(zip(states, states[1:], strict=False), 1):
        response = transition(
            evidence_client,
            tenant,
            source,
            target,
            version,
            **({"evidence_ref": row["id"]} if target == "RETIRED" else {}),
        )
        assert response.status_code == 201, response.text
        assert response.json()["result_version"] == version + 1
    final = evidence_client.get(f"/assets/{tenant.asset_id}", headers=headers(tenant)).json()
    assert final["current_state"] == "RETIRED" and final["version"] == len(states)
    for field in (
        "current_location_id",
        "owner_party_id",
        "custodian_party_id",
        "current_assignment_id",
    ):
        assert final[field] == initial[field]
    assert (
        transition(
            evidence_client, tenant, "RETIRED", "IN_STOCK", len(states), evidence_ref=row["id"]
        ).status_code
        == 422
    )
    assert evidence_client.get("/health/corrections", headers=headers(tenant)).json() == []


def test_arbitrary_missing_foreign_wrong_role_and_wrong_asset_evidence_rejects(
    evidence_client, asset_data, seed_asset
):
    a, b = asset_data
    row = upload(evidence_client, a).json()
    foreign = upload(evidence_client, b, b"foreign").json()
    assert transition(evidence_client, a, "RECEIVED", "IN_STOCK", 1).status_code == 201
    for evidence, expected in [
        (None, 422),
        (str(uuid7()), 404),
        (foreign["id"], 404),
        (row["id"], 422),
    ]:
        assert (
            transition(
                evidence_client, a, "IN_STOCK", "RETIRED", 2, evidence_ref=evidence
            ).status_code
            == expected
        )
    assert link(evidence_client, a, row["id"], role="OTHER").status_code == 201
    assert (
        transition(evidence_client, a, "IN_STOCK", "RETIRED", 2, evidence_ref=row["id"]).status_code
        == 422
    )
    other_asset = seed_asset(a)
    assert link(evidence_client, a, row["id"], entity_id=other_asset["id"]).status_code == 201
    assert (
        transition(evidence_client, a, "IN_STOCK", "RETIRED", 2, evidence_ref=row["id"]).status_code
        == 422
    )
    result = evidence_client.get(f"/assets/{a.asset_id}", headers=headers(a)).json()
    assert result["current_state"] == "IN_STOCK" and result["version"] == 2


def test_configuration_evidence_is_verified_without_asset_version_change(
    evidence_client, asset_data
):
    tenant = asset_data[0]
    row = upload(evidence_client, tenant).json()
    values = dict(
        image_name="base",
        image_version="1",
        config_profile="floor",
        applied_at=WHEN,
        evidence_ref=row["id"],
    )
    path = f"/assets/{tenant.asset_id}/configurations"
    assert evidence_client.post(path, json=values, headers=headers(tenant)).status_code == 422
    assert link(evidence_client, tenant, row["id"], role="CONFIG_EVIDENCE").status_code == 201
    result = evidence_client.post(path, json=values, headers=headers(tenant))
    assert result.status_code == 201, result.text
    assert result.json()["evidence_ref"] == row["id"]
    assert (
        link(
            evidence_client,
            tenant,
            row["id"],
            role="CONFIG_EVIDENCE",
            entity_type="ASSET_CONFIGURATION",
            entity_id=result.json()["id"],
        ).status_code
        == 201
    )
    assert (
        evidence_client.get(f"/assets/{tenant.asset_id}", headers=headers(tenant)).json()["version"]
        == 1
    )


def test_transition_correction_retains_evidence_in_reversal_and_checks_retirement(
    evidence_client, asset_data
):
    tenant = asset_data[0]
    assert transition(evidence_client, tenant, "RECEIVED", "IN_STOCK", 1).status_code == 201
    ordinary = transition(evidence_client, tenant, "IN_STOCK", "CONFIGURING", 2).json()
    row = upload(evidence_client, tenant).json()
    assert link(evidence_client, tenant, row["id"]).status_code == 201
    path = f"/assets/{tenant.asset_id}/transitions/{ordinary['id']}/corrections"
    values = dict(
        expected_version=3,
        to_state="RETIRED",
        reason="Correct recorded destination",
        correction_occurred_at=WHEN,
        evidence_ref=row["id"],
    )
    corrected = evidence_client.post(path, headers=headers(tenant), json=values)
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["result_version"] == 5
    values.update(expected_version=5, to_state="CONFIGURING", evidence_ref=None)
    next_generation = evidence_client.post(path, headers=headers(tenant), json=values)
    assert next_generation.status_code == 201, next_generation.text
    history = evidence_client.get(
        f"/assets/{tenant.asset_id}/transitions", headers=headers(tenant)
    ).json()
    assert next(h for h in history if h["result_version"] == 6)["evidence_ref"] == row["id"]
    assert evidence_client.get("/health/corrections", headers=headers(tenant)).json() == []


def test_direct_runtime_sql_cannot_retire_without_disposal_link(asset_data, app_connection):
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        call_transition(app_connection, tenant)
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError) as failure:
            call_transition(
                app_connection,
                tenant,
                expected_version=2,
                from_state="IN_STOCK",
                to_state="RETIRED",
                evidence_ref=None,
            )
        assert failure.value.orig.sqlstate == "23514"
    finally:
        app_connection.rollback()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert (
            app_connection.execute(
                text("SELECT version FROM fleetops.assets WHERE id=:id"), dict(id=tenant.asset_id)
            ).scalar_one()
            == 2
        )
