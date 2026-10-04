"""Exact handoff acceptance and boundary cases not implied by happy-path pair tests."""

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
from uuid6 import uuid7

from fleetops.auth import token_digest
from fleetops.db import metadata as db
from fleetops.domain import asset_facts, corrections
from fleetops.domain.assets import AssetConflict, AssetInvalid


def test_exact_movement_acceptance_with_distinct_correcting_actor(
    asset_data,
    app_connection,
    migrator_connection,
    space_password_hash,
):
    tenant = asset_data[0]
    correcting = SimpleNamespace(
        org_id=tenant.org_id, actor_id=uuid7(), raw_token=secrets.token_urlsafe(32)
    )
    user_id, destination = uuid7(), uuid7()
    migrator_connection.execute(
        db.actors.insert().values(
            id=correcting.actor_id,
            org_id=tenant.org_id,
            type="HUMAN",
            display_name="Correcting operator",
            created_by_actor_id=tenant.actor_id,
        )
    )
    migrator_connection.execute(
        db.users.insert().values(
            id=user_id,
            org_id=tenant.org_id,
            actor_id=correcting.actor_id,
            username="correcting-operator",
            password_hash=space_password_hash,
        )
    )
    migrator_connection.execute(
        db.sessions.insert().values(
            id=uuid7(),
            org_id=tenant.org_id,
            user_id=user_id,
            token_digest=token_digest(correcting.raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    migrator_connection.execute(
        db.locations.insert().values(
            id=destination,
            org_id=tenant.org_id,
            facility_id=tenant.facility_id,
            code="C",
            name="Correct destination",
            kind="BIN",
            created_by_actor_id=tenant.actor_id,
        )
    )
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        original = dict(
            asset_facts.move_asset(
                app_connection,
                tenant.asset_id,
                values=dict(
                    expected_version=1,
                    to_location_id=tenant.other_location_id,
                    reason="Mis-keyed move",
                    occurred_at=WHEN,
                ),
            )
        )
    corrected_time = WHEN - timedelta(days=2)
    with app_connection.begin():
        set_authenticated(app_connection, correcting)
        replacement = corrections.correct_movement(
            app_connection,
            tenant.asset_id,
            original["id"],
            values=dict(
                expected_version=2,
                to_location_id=destination,
                reason="\u00a0 Correct destination \u2007",
                occurred_at=corrected_time,
                correction_occurred_at=WHEN + timedelta(days=1),
            ),
        )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        raw = (
            app_connection.execute(
                select(db.asset_movements)
                .where(db.asset_movements.c.asset_id == tenant.asset_id)
                .order_by(db.asset_movements.c.result_version)
            )
            .mappings()
            .all()
        )
        assert len(raw) == 3 and dict(raw[0]) == original
        assert [row["result_version"] for row in raw] == [2, 3, 4]
        assert [row["correction_role"] for row in raw] == ["NONE", "REVERSAL", "CORRECTED"]
        assert [(row["from_location_id"], row["to_location_id"]) for row in raw] == [
            (tenant.location_id, tenant.other_location_id),
            (tenant.location_id, tenant.other_location_id),
            (tenant.location_id, destination),
        ]
        assert raw[0]["actor_id"] == tenant.actor_id
        assert raw[1]["actor_id"] == raw[2]["actor_id"] == correcting.actor_id
        for field in (
            "corrects_movement_id",
            "correction_pair_id",
            "correction_generation",
            "reason",
            "correction_occurred_at",
        ):
            assert raw[1][field] == raw[2][field]
        assert raw[2]["reason"] == "Correct destination"
        assert raw[2]["occurred_at"] == corrected_time
        assert raw[1]["occurred_at"] == original["occurred_at"]
        assert raw[2]["recorded_at"] != raw[2]["correction_occurred_at"]
        current = (
            app_connection.execute(select(db.assets).where(db.assets.c.id == tenant.asset_id))
            .mappings()
            .one()
        )
        assert current["current_location_id"] == destination and current["version"] == 4
        effective = (
            app_connection.execute(
                text("SELECT id FROM fleetops.effective_asset_movements WHERE asset_id=:id"),
                dict(id=tenant.asset_id),
            )
            .scalars()
            .all()
        )
        assert effective == [replacement["id"]]
        assert not asset_facts.reconcile_assets(app_connection)
    with app_connection.begin():
        set_authenticated(app_connection, correcting)
        repeat = corrections.correct_movement(
            app_connection,
            tenant.asset_id,
            original["id"],
            values=dict(
                expected_version=4,
                to_location_id=destination,
                reason="Retain corrected time",
                correction_occurred_at=WHEN,
            ),
        )
        assert repeat["occurred_at"] == corrected_time
        assert (
            repeat["corrects_movement_id"] == original["id"]
            and repeat["correction_generation"] == 2
        )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        asset_facts.change_custody(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=6,
                to_custodian_party_id=None,
                reason="Later unrelated event",
                occurred_at=WHEN,
            ),
        )
    with pytest.raises(AssetConflict):
        with app_connection.begin():
            set_authenticated(app_connection, correcting)
            corrections.correct_movement(
                app_connection,
                tenant.asset_id,
                original["id"],
                values=dict(
                    expected_version=7,
                    to_location_id=None,
                    reason="No replay",
                    correction_occurred_at=WHEN,
                ),
            )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert len(asset_facts.list_movements(app_connection, tenant.asset_id)) == 5
        assert (
            app_connection.execute(
                select(db.assets.c.version).where(db.assets.c.id == tenant.asset_id)
            ).scalar_one()
            == 7
        )


@pytest.mark.parametrize(
    "reason", [None, "", "\u00a0\u2007\u202f", "\u0085\u001c\u3000", "x" * 4001]
)
def test_durable_reason_validation_without_request_model(reason, asset_data, app_connection):
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = asset_facts.move_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                to_location_id=tenant.other_location_id,
                reason="Original",
                occurred_at=WHEN,
            ),
        )
    with pytest.raises(AssetInvalid):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            corrections.correct_movement(
                app_connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    expected_version=2,
                    to_location_id=None,
                    reason=reason,
                    correction_occurred_at=WHEN,
                ),
            )


@pytest.mark.parametrize("terminal", ["RESOLVED", "WAIVED"])
def test_terminal_workflow_rejects_every_followup_and_requires_note(
    terminal,
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], unit=None, condition="DAMAGED"
                )
            ],
        ),
    )
    url = f"/exceptions/{captured['exceptions'][0]['id']}"
    for note in (None, "", "\u00a0\u2007"):
        result = receiving_client.post(
            url + "/events",
            headers=headers(tenant),
            json=json_data(
                dict(expected_status="OPEN", to_status=terminal, note=note, occurred_at=WHEN)
            ),
        )
        assert result.status_code in (409, 422), result.text
    accepted = receiving_client.post(
        url + "/events",
        headers=headers(tenant),
        json=json_data(
            dict(expected_status="OPEN", to_status=terminal, note="Disposition", occurred_at=WHEN)
        ),
    )
    assert accepted.status_code == 201, accepted.text
    before = receiving_client.get(url, headers=headers(tenant)).json()
    for target in ("OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"):
        denied = receiving_client.post(
            url + "/events",
            headers=headers(tenant),
            json=json_data(
                dict(
                    expected_status=terminal,
                    to_status=target,
                    note="Cannot reopen",
                    occurred_at=WHEN,
                )
            ),
        )
        assert denied.status_code in (409, 422), denied.text
    assert receiving_client.get(url, headers=headers(tenant)).json() == before


@pytest.mark.parametrize("prior_status", ["OPEN", "ACKNOWLEDGED"])
def test_maximum_correction_reason_remains_complete_in_auto_resolution(
    prior_status,
    space_data,
    make_item,
    make_order,
    receiving_client,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], unit=None, condition="DAMAGED"
                )
            ],
        ),
    )
    exception_url = f"/exceptions/{captured['exceptions'][0]['id']}"
    if prior_status == "ACKNOWLEDGED":
        acknowledged = receiving_client.post(
            exception_url + "/events",
            headers=headers(tenant),
            json=json_data(
                dict(
                    expected_status="OPEN",
                    to_status="ACKNOWLEDGED",
                    note="Inspecting",
                    occurred_at=WHEN,
                )
            ),
        )
        assert acknowledged.status_code == 201, acknowledged.text
    reason = "R" * 4000
    response = receiving_client.post(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                condition="GOOD",
                reason=" \t" + reason + "\n ",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert response.status_code == 201, response.text
    workflow = receiving_client.get(
        f"/exceptions/{captured['exceptions'][0]['id']}", headers=headers(tenant)
    ).json()
    # Reference the reason rather than prefixing/truncating a maximum-length
    # operator statement. Both pair members must still retain all 4,000 chars.
    pair_id = response.json()["correction_pair_id"]
    expected_note = (
        "Corrected receipt reality superseded this observation's assertion. "
        f"Reason: see receipt-line correction pair {pair_id}."
    )
    assert workflow["resolution_note"] == expected_note
    assert workflow["events"][-1]["note"] == expected_note
    assert 1 <= len(expected_note) <= 4000
    assert workflow["events"][-1]["from_status"] == prior_status
    assert workflow["events"][-1]["evaluation_id"] is not None
    assert response.json()["reason"] == reason
    history = receiving_client.get(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        headers=headers(tenant),
    )
    assert history.status_code == 200, history.text
    assert len(history.json()) == 2
    assert {row["reason"] for row in history.json()} == {reason}
    assert {row["correction_pair_id"] for row in history.json()} == {pair_id}
    health = receiving_client.get("/health/corrections", headers=headers(tenant))
    assert health.status_code == 200, health.text
    assert health.json() == []
