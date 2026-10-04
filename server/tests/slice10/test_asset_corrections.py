"""Correction pairs replace authority without fabricating a physical inverse action."""

from datetime import UTC, datetime, timedelta

import pytest
from server.tests.auth_context import set_authenticated
from sqlalchemy import select

from fleetops.db.metadata import asset_movements, assets
from fleetops.domain import asset_facts, corrections
from fleetops.domain.assets import AssetConflict

WHEN = datetime(2026, 9, 12, tzinfo=UTC)


def move(connection, tenant, expected, destination):
    return asset_facts.move_asset(
        connection,
        tenant.asset_id,
        values=dict(
            expected_version=expected,
            to_location_id=destination,
            reason="Physical move",
            occurred_at=WHEN,
        ),
    )


def test_repeat_pair_preserves_original_and_pre_root_source(asset_data, app_connection):
    tenant = asset_data[0]
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    original = dict(move(app_connection, tenant, 1, tenant.other_location_id))
    app_connection.commit()
    for generation, destination in ((1, None), (2, tenant.location_id)):
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        corrected = corrections.correct_movement(
            app_connection,
            tenant.asset_id,
            original["id"],
            values=dict(
                expected_version=2 * generation,
                reason="  Correct destination  ",
                correction_occurred_at=WHEN - timedelta(days=generation),
                to_location_id=destination,
            ),
        )
        app_connection.commit()
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        rows = (
            app_connection.execute(
                select(asset_movements)
                .where(asset_movements.c.asset_id == tenant.asset_id)
                .order_by(asset_movements.c.result_version)
            )
            .mappings()
            .all()
        )
        assert dict(rows[0]) == original
        assert len(rows) == 1 + 2 * generation
        reversal = rows[-2]
        assert reversal["correction_role"] == "REVERSAL"
        assert reversal["from_location_id"] == tenant.location_id
        assert reversal["to_location_id"] == (tenant.other_location_id if generation == 1 else None)
        assert corrected["from_location_id"] == tenant.location_id
        assert corrected["to_location_id"] == destination
        assert corrected["correction_generation"] == generation
        assert corrected["result_version"] == 2 + 2 * generation
        assert corrected["occurred_at"] == WHEN
        assert reversal["reason"] == corrected["reason"] == "Correct destination"
        assert reversal["correction_pair_id"] == corrected["correction_pair_id"]
        assert reversal["actor_id"] == corrected["actor_id"] == tenant.actor_id
        assert not asset_facts.reconcile_assets(app_connection)
        app_connection.commit()
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    successor = move(app_connection, tenant, 6, tenant.other_location_id)
    assert successor["from_location_id"] == tenant.location_id
    app_connection.rollback()


def test_unrelated_later_global_event_blocks_correction(asset_data, app_connection):
    tenant = asset_data[0]
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    root = move(app_connection, tenant, 1, tenant.other_location_id)
    asset_facts.change_custody(
        app_connection,
        tenant.asset_id,
        values=dict(
            expected_version=2, to_custodian_party_id=None, reason="Custody ends", occurred_at=WHEN
        ),
    )
    app_connection.commit()
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    with pytest.raises(AssetConflict):
        corrections.correct_movement(
            app_connection,
            tenant.asset_id,
            root["id"],
            values=dict(
                expected_version=3,
                reason="Fix older move",
                correction_occurred_at=WHEN,
                to_location_id=None,
            ),
        )
    app_connection.rollback()
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    assert (
        app_connection.execute(
            select(assets.c.version).where(assets.c.id == tenant.asset_id)
        ).scalar_one()
        == 3
    )
    assert len(asset_facts.list_movements(app_connection, tenant.asset_id)) == 1


@pytest.mark.parametrize("kind", ["transition", "movement", "ownership", "custody", "assignment"])
def test_all_asset_classes_repeat_and_reject_member_roots(kind, asset_data, app_connection):
    """Each class uses the shared global head while retaining its original pre-root source."""
    from fleetops.db import metadata as db
    from fleetops.domain import assets as asset_service
    from fleetops.domain import assignments
    from fleetops.domain.assets import AssetInvalid

    tenant = asset_data[0]
    settings = {
        "transition": (
            db.asset_transitions,
            "corrects_transition_id",
            "current_state",
            dict(to_state="ON_HOLD"),
            "ON_HOLD",
        ),
        "movement": (
            db.asset_movements,
            "corrects_movement_id",
            "current_location_id",
            dict(to_location_id=None),
            None,
        ),
        "ownership": (
            db.asset_ownership_changes,
            "corrects_ownership_change_id",
            "owner_party_id",
            dict(to_owner_party_id=tenant.other_manufacturer_id),
            tenant.other_manufacturer_id,
        ),
        "custody": (
            db.asset_custody_changes,
            "corrects_custody_change_id",
            "custodian_party_id",
            dict(to_custodian_party_id=None),
            None,
        ),
        "assignment": (
            db.asset_assignment_events,
            "corrects_assignment_event_id",
            "current_assignment_id",
            dict(to_assignee_type="LOCATION", to_assignee_id=tenant.location_id),
            None,
        ),
    }
    table, root_field, projection, replacement, destination = settings[kind]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        values = dict(expected_version=1, reason="Original fact", occurred_at=WHEN)
        if kind == "transition":
            root = asset_service.transition_asset(
                app_connection,
                tenant.asset_id,
                values=dict(values, from_state="RECEIVED", to_state="IN_STOCK"),
            )
        elif kind == "movement":
            root = move(app_connection, tenant, 1, tenant.other_location_id)
        elif kind == "ownership":
            root = asset_facts.change_ownership(
                app_connection,
                tenant.asset_id,
                values=dict(values, to_owner_party_id=tenant.party_id),
            )
        elif kind == "custody":
            root = asset_facts.change_custody(
                app_connection,
                tenant.asset_id,
                values=dict(values, to_custodian_party_id=tenant.vendor_id),
            )
        else:
            root = assignments.assign_asset(
                app_connection,
                tenant.asset_id,
                values=dict(values, assignee_type="ACTOR", assignee_id=tenant.actor_id),
            )
        original = dict(root)
    correct = getattr(corrections, "correct_" + kind)
    for generation in (1, 2):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            produced = correct(
                app_connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    replacement,
                    expected_version=2 * generation,
                    reason="Corrected assertion",
                    correction_occurred_at=WHEN,
                ),
            )
            assert produced[root_field] == root["id"]
            assert produced["correction_generation"] == generation
            assert produced["result_version"] == 2 + 2 * generation
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            stored = (
                app_connection.execute(select(assets).where(assets.c.id == tenant.asset_id))
                .mappings()
                .one()
            )
            assert stored[projection] == (produced["id"] if kind == "assignment" else destination)
            assert (
                dict(
                    app_connection.execute(select(table).where(table.c.id == root["id"]))
                    .mappings()
                    .one()
                )
                == original
            )
            assert not asset_facts.reconcile_assets(app_connection)
    with pytest.raises(AssetInvalid):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            correct(
                app_connection,
                tenant.asset_id,
                produced["id"],
                values=dict(
                    replacement,
                    expected_version=6,
                    reason="Member is not a root",
                    correction_occurred_at=WHEN,
                ),
            )


def test_assignment_intervals_use_replacement_domain_time(asset_data, app_connection):
    from fleetops.domain import assignments

    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        first = assignments.assign_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                reason="First assignment",
                occurred_at=WHEN,
                assignee_type="ACTOR",
                assignee_id=tenant.actor_id,
            ),
        )
        second = assignments.assign_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=2,
                reason="Reassignment",
                occurred_at=WHEN + timedelta(days=1),
                assignee_type="PARTY",
                assignee_id=tenant.vendor_id,
            ),
        )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        corrected = corrections.correct_assignment(
            app_connection,
            tenant.asset_id,
            second["id"],
            values=dict(
                expected_version=3,
                to_assignee_type=None,
                to_assignee_id=None,
                reason="Assignment actually ended",
                correction_occurred_at=WHEN + timedelta(days=10),
                occurred_at=WHEN - timedelta(days=1),
            ),
        )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        history = assignments.assignment_history(app_connection, tenant.asset_id)
        assert len(history["events"]) == 4
        assert history["intervals"] == [
            dict(
                establishing_event_id=first["id"],
                assignee_type="ACTOR",
                assignee_id=tenant.actor_id,
                started_at=WHEN,
                ended_at=WHEN - timedelta(days=1),
            )
        ]
        assert corrected["occurred_at"] < first["occurred_at"]
        assert not asset_facts.reconcile_assets(app_connection)


def test_lifecycle_replacement_graph_and_creation_boundaries(asset_data, app_connection):
    from fleetops.domain import assets as asset_service
    from fleetops.domain.lifecycle import LifecycleInvalid

    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = asset_service.transition_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                from_state="RECEIVED",
                to_state="IN_STOCK",
                reason="Original state",
                occurred_at=WHEN,
            ),
        )
    for state in ("RECEIVED", "DEPLOYED", "RETIRED"):
        with pytest.raises(LifecycleInvalid):
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                corrections.correct_transition(
                    app_connection,
                    tenant.asset_id,
                    root["id"],
                    values=dict(
                        expected_version=2,
                        to_state=state,
                        reason="Illegal replacement",
                        correction_occurred_at=WHEN,
                    ),
                )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        corrected = corrections.correct_transition(
            app_connection,
            tenant.asset_id,
            root["id"],
            values=dict(
                expected_version=2,
                to_state="ON_HOLD",
                reason="Actual hold",
                correction_occurred_at=WHEN,
            ),
        )
        ordinary = asset_service.transition_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=4,
                from_state="ON_HOLD",
                to_state="RECEIVED",
                reason="Leave actual hold",
                occurred_at=WHEN,
            ),
        )
        assert ordinary["result_version"] == 5
        assert corrected["from_state"] == "RECEIVED"
