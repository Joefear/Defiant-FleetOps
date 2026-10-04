"""Durable malformed-authority rejection and preserved workflow/creation boundaries."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)
from server.tests.slice10.test_security import isolated_history_insert
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db import metadata as db
from fleetops.domain import asset_facts, assets, corrections
from fleetops.domain.assets import AssetConflict


@pytest.mark.parametrize(
    "fault",
    [
        "reason",
        "pair",
        "generation",
        "skip",
        "version",
        "reversal_payload",
        "replacement_source",
        "duplicate_role",
    ],
)
def test_forged_asset_pair_cannot_commit(fault, asset_data, app_connection, migrator_connection):
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
    shared = dict(
        org_id=tenant.org_id,
        asset_id=tenant.asset_id,
        from_location_id=tenant.location_id,
        to_location_id=tenant.other_location_id,
        actor_id=tenant.actor_id,
        reason="Pair",
        occurred_at=WHEN,
        corrects_movement_id=root["id"],
        correction_pair_id=uuid7(),
        correction_generation=1,
        correction_occurred_at=WHEN,
    )
    reversal = dict(shared, id=uuid7(), result_version=3, correction_role="REVERSAL")
    corrected = dict(
        shared, id=uuid7(), result_version=4, correction_role="CORRECTED", to_location_id=None
    )
    if fault == "reason":
        corrected["reason"] = "Other reason"
    elif fault == "pair":
        corrected["correction_pair_id"] = uuid7()
    elif fault == "generation":
        corrected["correction_generation"] = 2
    elif fault == "skip":
        reversal["correction_generation"] = corrected["correction_generation"] = 2
    elif fault == "version":
        corrected["result_version"] = 5
    elif fault == "reversal_payload":
        reversal["to_location_id"] = None
    elif fault == "replacement_source":
        corrected["from_location_id"] = tenant.other_location_id
    elif fault == "duplicate_role":
        corrected["correction_role"] = "REVERSAL"
    with isolated_history_insert(migrator_connection, app_connection):
        with pytest.raises(DBAPIError) as denied:
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                app_connection.execute(db.asset_movements.insert().values(reversal))
                app_connection.execute(db.asset_movements.insert().values(corrected))
        assert denied.value.orig.sqlstate in ("23514", "23505")
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert app_connection.execute(
            select(db.asset_movements.c.id).where(db.asset_movements.c.asset_id == tenant.asset_id)
        ).scalars().all() == [root["id"]]


@pytest.mark.parametrize(
    "fault",
    ["location_projection", "owner_projection", "missing_baseline", "missing_assignment_witness"],
)
def test_correction_cannot_repair_corrupt_authority(
    fault, asset_data, app_connection, migrator_connection
):
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
    if fault == "location_projection":
        migrator_connection.execute(
            db.assets.update()
            .where(db.assets.c.id == tenant.asset_id)
            .values(current_location_id=None)
        )
    elif fault == "owner_projection":
        migrator_connection.execute(
            db.assets.update()
            .where(db.assets.c.id == tenant.asset_id)
            .values(owner_party_id=tenant.party_id)
        )
    else:
        table = (
            db.asset_initial_facts
            if fault == "missing_baseline"
            else db.asset_initial_assignment_facts
        )
        migrator_connection.execute(table.delete().where(table.c.asset_id == tenant.asset_id))
    migrator_connection.commit()
    before = snapshot(migrator_connection, tenant.org_id, ASSET_TABLES)
    with pytest.raises(AssetConflict):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            corrections.correct_movement(
                app_connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    expected_version=2,
                    to_location_id=None,
                    reason="Cannot repair",
                    correction_occurred_at=WHEN,
                ),
            )
    assert snapshot(migrator_connection, tenant.org_id, ASSET_TABLES) == before


def test_illegal_corrected_lifecycle_health_is_python_owned(
    asset_data,
    app_connection,
    migrator_connection,
    receiving_client,
):
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = assets.transition_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                from_state="RECEIVED",
                to_state="IN_STOCK",
                reason="Original",
                occurred_at=WHEN,
            ),
        )
        corrected = corrections.correct_transition(
            app_connection,
            tenant.asset_id,
            root["id"],
            values=dict(
                expected_version=2,
                to_state="ON_HOLD",
                reason="Replacement",
                correction_occurred_at=WHEN,
            ),
        )
    migrator_connection.execute(
        db.asset_transitions.update()
        .where(db.asset_transitions.c.id == corrected["id"])
        .values(to_state="DEPLOYED")
    )
    migrator_connection.execute(
        db.assets.update().where(db.assets.c.id == tenant.asset_id).values(current_state="DEPLOYED")
    )
    migrator_connection.commit()
    response = receiving_client.get("/health/corrections", headers=headers(tenant))
    assert response.status_code == 200, response.text
    assert "illegal_corrected_lifecycle" in {issue["issue"] for issue in response.json()}
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert (
            "illegal_corrected_lifecycle"
            in asset_facts.reconcile_assets(app_connection)[0]["discrepancies"]
        )
    with pytest.raises(AssetConflict):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            corrections.correct_transition(
                app_connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    expected_version=4,
                    to_state="ON_HOLD",
                    reason="Cannot hide corruption",
                    correction_occurred_at=WHEN,
                ),
            )


@pytest.mark.parametrize("prior_status", ["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"])
def test_still_true_exception_keeps_status_and_observation(
    prior_status, space_data, make_item, make_order, receiving_client
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
    if prior_status != "OPEN":
        event = receiving_client.post(
            url + "/events",
            headers=headers(tenant),
            json=json_data(
                dict(
                    expected_status="OPEN",
                    to_status=prior_status,
                    note="Disposition",
                    occurred_at=WHEN,
                )
            ),
        )
        assert event.status_code == 201, event.text
    before = receiving_client.get(url, headers=headers(tenant)).json()
    response = receiving_client.post(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                notes="Correct descriptive note",
                reason="No change to damage",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert response.status_code == 201, response.text
    assert receiving_client.get(url, headers=headers(tenant)).json() == before
    observed = receiving_client.get(f"/receipts/{captured['id']}", headers=headers(tenant)).json()[
        "exceptions"
    ]
    assert observed == captured["exceptions"]


def test_safe_serialized_line_correction_preserves_entire_creation_bundle(
    space_data,
    receiving_client,
    migrator_connection,
):
    tenant = space_data[0]
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(tenant, lines=[line_data(tenant, condition="DAMAGED")]),
    )
    before = snapshot(migrator_connection, tenant.org_id, ASSET_TABLES)
    response = receiving_client.post(
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                condition="GOOD",
                reason="Actual packaging condition",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert response.status_code == 201, response.text
    assert snapshot(migrator_connection, tenant.org_id, ASSET_TABLES) == before


def test_new_definer_inventory_is_category_one_narrow_and_fail_closed(
    migrator_connection, app_connection
):
    names = [
        "correct_transition",
        "correct_movement",
        "correct_custody",
        "correct_ownership",
        "correct_assignment",
        "initialize_exception_workflow",
        "transition_exception",
    ]
    rows = (
        migrator_connection.execute(
            text("""SELECT p.proname,p.prosecdef,p.proconfig,
        pg_get_userbyid(p.proowner) AS owner,p.proargnames,p.oid::regprocedure::text AS signature,
        oidvectortypes(p.proargtypes) AS types,
        has_function_privilege('fleetops_authenticator',p.oid,'EXECUTE') AS auth_execute,
        EXISTS(SELECT 1 FROM aclexplode(p.proacl) a WHERE a.grantee=0) AS public_execute
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops' AND p.proname=ANY(:names)"""),
            dict(names=names),
        )
        .mappings()
        .all()
    )
    assert {row["proname"] for row in rows} == set(names)
    for row in rows:
        assert row["prosecdef"] and row["owner"] == "fleetops_migrator"
        assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
        assert not row["auth_execute"] and not row["public_execute"]
        assert not any("actor" in name for name in (row["proargnames"] or []))
        if row["proname"] == "initialize_exception_workflow":
            continue
        arguments = ",".join("NULL::" + typ.strip() for typ in row["types"].split(","))
        with pytest.raises(DBAPIError) as denied:
            with app_connection.begin():
                app_connection.exec_driver_sql(f"SELECT fleetops.{row['proname']}({arguments})")
        assert denied.value.orig.sqlstate == "42501"
