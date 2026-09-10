"""Actual PostgreSQL lock waits prove one global winner across assignment operation classes."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import call_transition
from server.tests.slice5.test_transitions import wait_blocked
from server.tests.slice6.conftest import MOVE, OWNERSHIP, call_fact
from server.tests.slice7.conftest import call_assignment, runtime_assignment
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from fleetops.db.metadata import assets


def operate(connection, a, kind, expected):
    """Choose only real SQL functions; no simulated version producers enter the race."""
    if kind == "transition":
        return call_transition(connection, a, expected_version=expected)
    if kind in {"movement", "ownership"}:
        return call_fact(
            connection, a, MOVE if kind == "movement" else OWNERSHIP, expected_version=expected
        )
    return call_assignment(
        connection,
        a,
        expected_version=expected,
        unassign=kind == "unassign",
        assignee_type="PARTY" if kind == "assign-other" else "LOCATION",
        assignee_id=a.party_id if kind == "assign-other" else a.location_id,
    )


def contender(engine, a, pids, kind, expected):
    """Each contender has a distinct app login transaction and reports committed outcome only."""
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            assert connection.exec_driver_sql("SELECT current_user").scalar_one() == "fleetops_app"
            set_authenticated(connection, a)
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            row = operate(connection, a, kind, expected)
        return "success", dict(row)
    except DBAPIError as error:
        return error.orig.sqlstate, error.orig.diag.message_primary


def assert_winner(before, after, winner):
    """Compare all persisted facts so loser writes or orthogonal changes cannot hide."""
    if "to_assignee_id" in winner:
        table, projection = "asset_assignment_events", "current_assignment_id"
        target = winner["id"] if winner["to_assignee_id"] else None
    elif "to_state" in winner:
        table, projection, target = "asset_transitions", "current_state", winner["to_state"]
    elif "to_location_id" in winner:
        table, projection, target = (
            "asset_movements",
            "current_location_id",
            winner["to_location_id"],
        )
    else:
        table, projection, target = (
            "asset_ownership_changes",
            "owner_party_id",
            winner["to_owner_party_id"],
        )
    assert winner["result_version"] == before["asset"]["version"] + 1
    assert after == before | {
        "asset": before["asset"] | {projection: target, "version": winner["result_version"]},
        table: before[table] + [winner],
    }


@pytest.mark.parametrize(
    "first,second,assigned",
    [
        ("assign", "assign-other", False),
        ("assign", "unassign", True),
        ("assign", "transition", False),
        ("assign", "movement", False),
        ("unassign", "ownership", True),
    ],
)
def test_real_same_and_cross_class_races_have_one_global_winner(
    first,
    second,
    assigned,
    database,
    asset_data,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    if assigned:
        runtime_assignment(app_connection, a)
    before = assignment_snapshot(a)
    expected = before["asset"]["version"]
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool, hide_parameters=True)
    pids = Queue()
    try:
        migrator_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(contender, engine, a, pids, k, expected) for k in (first, second)
            ]
            try:
                waiting = [pids.get(timeout=10), pids.get(timeout=10)]
                assert len(set(waiting)) == 2
                wait_blocked(migrator_connection, waiting)
            finally:
                migrator_connection.commit()
            results = [f.result(timeout=20) for f in futures]
        assert sorted(r[0] for r in results) == ["40001", "success"]
        assert next(r for r in results if r[0] != "success") == ("40001", "Asset version is stale")
        assert_winner(
            before, assignment_snapshot(a), next(r[1] for r in results if r[0] == "success")
        )
    finally:
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("operation", ["assign", "unassign"])
def test_expected_version_waits_for_asset_lock_and_function_retains_it_until_commit(
    operation,
    database,
    asset_data,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    runtime_assignment(app_connection, a)
    before = assignment_snapshot(a)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool, hide_parameters=True)
    pids = Queue()
    try:
        transaction = app_connection.begin()
        set_authenticated(app_connection, a)
        app_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(contender, engine, a, pids, operation, 2)
            try:
                pid = pids.get(timeout=10)
                wait_blocked(migrator_connection, [pid])
                winner = operate(app_connection, a, "assign-other", 2)
                wait_blocked(migrator_connection, [pid])
                assert not pending.done()
            finally:
                transaction.commit()
            assert pending.result(timeout=20) == ("40001", "Asset version is stale")
        assert_winner(before, assignment_snapshot(a), winner)
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()
