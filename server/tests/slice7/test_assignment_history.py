"""Independent SQL, reconstruction, orthogonality and corruption proofs for ADR-008."""

from datetime import UTC, datetime

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import headers, runtime_transition
from server.tests.slice6.conftest import CUSTODY, MOVE, OWNERSHIP, for_asset, runtime_fact
from server.tests.slice6.test_fact_history import health
from server.tests.slice7.conftest import (
    event_values,
    runtime_assignment,
    runtime_configuration,
    witness_values,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_assignment_events as events
from fleetops.db.metadata import asset_initial_assignment_facts as witnesses
from fleetops.db.metadata import assets
from fleetops.domain.assignments import assignment_history


def test_witness_one_per_asset_immutable_creation_identity_and_non_versioning(
    asset_data,
    seed_asset,
    migrator_connection,
    assignment_snapshot,
):
    a = for_asset(asset_data[0], seed_asset(asset_data[0], initial_assignment=False)["id"])
    before = assignment_snapshot(a)
    witness = dict(
        migrator_connection.execute(
            witnesses.insert().values(witness_values(a)).returning(witnesses)
        )
        .mappings()
        .one()
    )
    migrator_connection.commit()
    after = assignment_snapshot(a)
    assert after == before | {witnesses.name: [witness]}
    assert set(witness) == {"asset_id", "org_id", "actor_id", "occurred_at", "recorded_at"}
    assert after["asset"]["version"] == 1
    assert [row["result_version"] for row in after["asset_transitions"]] == [1]
    with pytest.raises(DBAPIError) as error, migrator_connection.begin():
        migrator_connection.execute(witnesses.insert().values(witness_values(a)))
    assert error.value.orig.sqlstate == "23505"


def test_missing_witness_rejects_first_assignment_and_is_not_synthesized(
    asset_data,
    seed_asset,
    app_connection,
    assignment_snapshot,
):
    a = for_asset(asset_data[0], seed_asset(asset_data[0], initial_assignment=False)["id"])
    before = assignment_snapshot(a)
    for unassign in (False, True):
        with pytest.raises(DBAPIError) as error:
            runtime_assignment(app_connection, a, unassign=unassign)
        assert error.value.orig.sqlstate == "P0001"
        assert error.value.orig.diag.message_primary == "Initial assignment facts missing"
        assert assignment_snapshot(a) == before
    assert health(app_connection, a)[0]["discrepancies"] == ["missing_initial_assignment_facts"]


def test_assign_reassign_unassign_reconstructs_immutable_intervals_without_projection(
    asset_data,
    app_connection,
    assignment_snapshot,
    migrator_connection,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    # Deliberately disagreeing clocks prove that interval succession follows versions.
    times = [datetime(y, 1, 1, tzinfo=UTC) for y in (2099, 1970, 2050, 2000)]
    returned = []
    for expected, (kind, target, unassign, occurred) in enumerate(
        [
            ("ACTOR", a.actor_id, False, times[0]),
            ("LOCATION", a.location_id, False, times[1]),
            (None, None, True, times[2]),
            ("PARTY", a.party_id, False, times[3]),
        ],
        1,
    ):
        old = assignment_snapshot(a)
        row = runtime_assignment(
            app_connection,
            a,
            expected_version=expected,
            assignee_type=kind,
            assignee_id=target,
            unassign=unassign,
            occurred_at=occurred,
        )
        returned.append(row)
        assert row["result_version"] == expected + 1
        assert row["actor_id"] == a.actor_id and row["id"].version == 7
        assert row["corrects_assignment_event_id"] is row["client_op_id"] is None
        assert (row["from_assignee_type"], row["from_assignee_id"]) == (
            (returned[-2]["to_assignee_type"], returned[-2]["to_assignee_id"])
            if expected > 1
            else (None, None)
        )
        assert (row["to_assignee_type"], row["to_assignee_id"]) == (kind, target)
        assert row["occurred_at"] == occurred and row["recorded_at"].tzinfo is not None
        new = assignment_snapshot(a)
        assert new == old | {
            "asset": old["asset"]
            | {"version": expected + 1, "current_assignment_id": None if unassign else row["id"]},
            events.name: old[events.name] + [row],
        }
    assert assignment_snapshot(a)[witnesses.name] == before[witnesses.name]
    assert health(app_connection, a) == []
    # Reading the corrupt projection must not contaminate the historical reconstruction.
    migrator_connection.execute(
        assets.update().where(assets.c.id == a.asset_id).values(current_assignment_id=None)
    )
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        history = assignment_history(app_connection, a.asset_id)
    assert [dict(row) for row in history["events"]] == returned
    assert history["initial_assignment_fact"] == before[witnesses.name][0]
    assert [(i["assignee_type"], i["started_at"], i["ended_at"]) for i in history["intervals"]] == [
        ("ACTOR", times[0], times[1]),
        ("LOCATION", times[1], times[2]),
        ("PARTY", times[3], None),
    ]


def test_unassigned_to_unassigned_is_rejected_without_writes(
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_assignment(app_connection, a, unassign=True)
    assert error.value.orig.sqlstate == "23514"
    assert assignment_snapshot(a) == before


def eight_versions(connection, a):
    """An assignment after UNASSIGN is ASSIGN under ADR-008, even when colloquially reassigned."""
    runtime_fact(connection, a, MOVE)
    runtime_assignment(connection, a, expected_version=2)
    runtime_fact(connection, a, CUSTODY, expected_version=3)
    runtime_transition(connection, a, expected_version=4)
    runtime_assignment(connection, a, expected_version=5, unassign=True)
    runtime_fact(connection, a, OWNERSHIP, expected_version=6)
    runtime_assignment(
        connection, a, expected_version=7, assignee_type="PARTY", assignee_id=a.party_id
    )


def test_complete_eight_version_sequence_excludes_both_witnesses_and_configurations(
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    eight_versions(app_connection, a)
    runtime_configuration(app_connection, a)
    after = assignment_snapshot(a)
    assert after["asset"]["version"] == 8
    assert after["initial"] == before["initial"]
    assert after[witnesses.name] == before[witnesses.name]
    assert [row["result_version"] for row in after[events.name]] == [3, 6, 8]
    with app_connection.begin():
        set_authenticated(app_connection, a)
        versions = (
            app_connection.execute(
                text("""
            SELECT result_version FROM (
                SELECT org_id, asset_id, result_version FROM fleetops.asset_transitions
                UNION ALL SELECT org_id, asset_id, result_version FROM fleetops.asset_movements
                UNION ALL SELECT org_id, asset_id, result_version
                FROM fleetops.asset_custody_changes
                UNION ALL SELECT org_id, asset_id, result_version
                FROM fleetops.asset_ownership_changes
                UNION ALL SELECT org_id, asset_id, result_version
                FROM fleetops.asset_assignment_events
            ) h WHERE org_id=:org AND asset_id=:asset ORDER BY result_version
        """),
                {"org": a.org_id, "asset": a.asset_id},
            )
            .scalars()
            .all()
        )
        assert versions == list(range(1, 9))
    assert health(app_connection, a) == []


def test_deployed_on_hold_preserves_current_assignment_and_creates_no_assignment_event(
    asset_data,
    app_connection,
    asset_client,
    assignment_snapshot,
):
    a = asset_data[0]
    current = "RECEIVED"
    for version, target in enumerate(("IN_STOCK", "CONFIGURING", "READY", "DEPLOYED"), 1):
        runtime_transition(
            app_connection, a, expected_version=version, from_state=current, to_state=target
        )
        current = target
    runtime_assignment(app_connection, a, expected_version=5)
    before = assignment_snapshot(a)
    response = asset_client.post(
        f"/assets/{a.asset_id}/transitions",
        headers=headers(a),
        json={
            "expected_version": 6,
            "from_state": "DEPLOYED",
            "to_state": "ON_HOLD",
            "reason": "Investigate",
            "occurred_at": "2026-07-01T00:00:00Z",
        },
    )
    assert response.status_code == 201, response.text
    after = assignment_snapshot(a)
    assert after[events.name] == before[events.name]
    assert after["asset"]["current_assignment_id"] == before["asset"]["current_assignment_id"]
    assert after["asset"]["current_state"] == "ON_HOLD"
    assert (
        asset_client.get(f"/assets/{a.asset_id}/assignments", headers=headers(a)).json()[
            "intervals"
        ][0]["ended_at"]
        is None
    )


@pytest.mark.parametrize(
    "corruption",
    ["assigned-null", "wrong-event", "unassigned-nonnull", "no-history", "foreign-asset"],
)
def test_corrupt_projection_rejects_next_operations_without_repair(
    corruption,
    asset_data,
    seed_asset,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    expected = 1
    category = "assignment_projection_mismatch"
    if corruption in {"no-history", "foreign-asset"}:
        other = for_asset(a, seed_asset(a)["id"])
        first = runtime_assignment(app_connection, other)
        bad = first["id"]
        if corruption == "foreign-asset":
            runtime_assignment(app_connection, a)
            expected = 2
        else:
            category = "assignment_projection_without_history"
        # The actual FK must first reject this; deliberate corruption then disables only RI.
        with pytest.raises(DBAPIError) as error, migrator_connection.begin():
            migrator_connection.execute(
                assets.update().where(assets.c.id == a.asset_id).values(current_assignment_id=bad)
            )
        assert error.value.orig.sqlstate == "23503"
        migrator_connection.exec_driver_sql(
            "ALTER TABLE fleetops.assets DROP CONSTRAINT fk_assets_assignment"
        )
    else:
        first = runtime_assignment(app_connection, a)
        expected = 2
        bad = None
        if corruption == "wrong-event":
            runtime_assignment(
                app_connection, a, expected_version=2, assignee_type="PARTY", assignee_id=a.party_id
            )
            expected, bad = 3, first["id"]
        elif corruption == "unassigned-nonnull":
            runtime_assignment(app_connection, a, expected_version=2, unassign=True)
            expected, bad = 3, first["id"]
    try:
        migrator_connection.execute(
            assets.update().where(assets.c.id == a.asset_id).values(current_assignment_id=bad)
        )
        migrator_connection.commit()
        before = assignment_snapshot(a)
        row = next(r for r in health(app_connection, a) if r["asset_id"] == a.asset_id)
        assert category in row["discrepancies"]
        if corruption in {"no-history", "foreign-asset"}:
            assert "assignment_target_invalid" in row["discrepancies"]
        for unassign in (False, True):
            with pytest.raises(DBAPIError) as error:
                runtime_assignment(app_connection, a, expected_version=expected, unassign=unassign)
            assert error.value.orig.sqlstate == "P0001"
            assert assignment_snapshot(a) == before
    finally:
        if corruption in {"no-history", "foreign-asset"}:
            migrator_connection.rollback()
            migrator_connection.execute(
                assets.update().where(assets.c.id == a.asset_id).values(current_assignment_id=None)
            )
            migrator_connection.exec_driver_sql("""
                ALTER TABLE fleetops.assets ADD CONSTRAINT fk_assets_assignment
                FOREIGN KEY (org_id, id, current_assignment_id)
                REFERENCES fleetops.asset_assignment_events (org_id, asset_id, id)
            """)
            migrator_connection.commit()


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "ahead"])
def test_assignment_global_corruption_detected_without_repair(
    corruption,
    asset_data,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    eight_versions(app_connection, a)
    if corruption == "missing":
        migrator_connection.execute(
            events.delete().where(events.c.asset_id == a.asset_id, events.c.result_version == 6)
        )
    else:
        version = 2 if corruption == "duplicate" else 20
        migrator_connection.execute(events.insert().values(event_values(a, result_version=version)))
    migrator_connection.commit()
    before = assignment_snapshot(a)
    row = health(app_connection, a)[0]
    field, category, value = {
        "missing": ("global_missing_version_ranges", "global_version_missing", [[6, 6]]),
        "duplicate": ("global_duplicate_versions", "global_version_duplicate", [2]),
        "ahead": ("global_ahead_versions", "global_history_version_ahead", [20]),
    }[corruption]
    assert row[field] == value and category in row["discrepancies"]
    if corruption == "ahead":
        assert "assignment_history_version_ahead" in row["discrepancies"]
        for unassign in (False, True):
            with pytest.raises(DBAPIError) as error:
                runtime_assignment(app_connection, a, expected_version=8, unassign=unassign)
            assert error.value.orig.sqlstate == "P0001"
    assert assignment_snapshot(a) == before


def test_later_events_remain_current_authority_but_missing_witness_still_reconciles(
    asset_data,
    app_connection,
    migrator_connection,
):
    a = asset_data[0]
    first = runtime_assignment(app_connection, a)
    migrator_connection.execute(witnesses.delete().where(witnesses.c.asset_id == a.asset_id))
    migrator_connection.commit()
    assert health(app_connection, a)[0]["discrepancies"] == ["missing_initial_assignment_facts"]
    second = runtime_assignment(app_connection, a, expected_version=2, unassign=True)
    assert second["from_assignee_id"] == first["to_assignee_id"]
    assert health(app_connection, a)[0]["discrepancies"] == ["missing_initial_assignment_facts"]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expected_version": None}, "40001"),
        ({"expected_version": 0}, "40001"),
        ({"reason": None}, "23502"),
        ({"reason": "\t\n"}, "23514"),
        ({"reason": "x" * 4001}, "23514"),
        ({"occurred_at": None}, "23502"),
        ({"event_id": None}, "23502"),
        ({"assignee_type": "USER"}, "23514"),
        ({"assignee_id": None}, "23514"),
        ({"assignee_type": None}, "23514"),
    ],
)
def test_failed_assignment_has_no_partial_writes(
    changes,
    code,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_assignment(app_connection, a, **changes)
    assert error.value.orig.sqlstate == code
    assert assignment_snapshot(a) == before


def test_client_operation_seam_does_not_deduplicate_assignments(
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    correlation = uuid7()
    first = runtime_assignment(app_connection, a, client_op_id=correlation)
    second = runtime_assignment(
        app_connection,
        a,
        expected_version=2,
        assignee_type="PARTY",
        assignee_id=a.party_id,
        client_op_id=correlation,
    )
    assert first["client_op_id"] == second["client_op_id"] == correlation
    assert assignment_snapshot(a)["asset"]["version"] == 3
