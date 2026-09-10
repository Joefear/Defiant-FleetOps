"""Four operation classes serialize through the existing Asset row, on real PostgreSQL."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import call_transition
from server.tests.slice5.test_transitions import wait_blocked
from server.tests.slice6.conftest import CUSTODY, KINDS, MOVE, OWNERSHIP, call_fact
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from fleetops.db.metadata import assets


def operate(connection, tenant, kind):
    """None selects the unchanged Slice 5 transition boundary, not a simulated producer."""
    return (
        call_transition(connection, tenant) if kind is None else call_fact(connection, tenant, kind)
    )


def contender(engine, tenant, pids, kind):
    """Return only a committed event or the actual PostgreSQL rejection after rollback."""
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            assert connection.exec_driver_sql("SELECT current_user").scalar_one() == "fleetops_app"
            set_authenticated(connection, tenant)
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            row = operate(connection, tenant, kind)
        return "success", row
    except DBAPIError as error:
        return error.orig.sqlstate, error.orig.diag.message_primary


def assert_only_winner(before, after, winner):
    """Compare the entire persisted result, so a losing class cannot leave any side effect."""
    if "to_state" in winner:
        table, projection, target = "asset_transitions", "current_state", winner["to_state"]
    else:
        kind = next(kind for kind in KINDS if kind.to_field in winner)
        table, projection, target = kind.table.name, kind.projection, winner[kind.to_field]
    assert winner["result_version"] == 2
    expected = before | {
        "asset": before["asset"] | {projection: target, "version": 2},
        table: before[table] + [winner],
    }
    assert after == expected


@pytest.mark.parametrize(
    "first,second",
    [
        (MOVE, MOVE),
        (CUSTODY, CUSTODY),
        (OWNERSHIP, OWNERSHIP),
        (None, MOVE),
        (MOVE, CUSTODY),
        (OWNERSHIP, MOVE),
    ],
    ids=[
        "movement-movement",
        "custody-custody",
        "ownership-ownership",
        "transition-movement",
        "movement-custody",
        "ownership-movement",
    ],
)
def test_real_same_and_cross_class_contention_has_one_global_winner(
    first,
    second,
    database,
    asset_data,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()
    try:
        # Hold one Asset gate until both independent runtime transactions are waiting.
        migrator_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(contender, engine, a, pids, kind) for kind in (first, second)
            ]
            try:
                waiting = [pids.get(timeout=10), pids.get(timeout=10)]
                assert len(set(waiting)) == 2
                wait_blocked(migrator_connection, waiting)
            finally:
                migrator_connection.commit()
            results = [future.result(timeout=20) for future in futures]
        assert sorted(result[0] for result in results) == ["40001", "success"]
        assert next(result for result in results if result[0] != "success") == (
            "40001",
            "Asset version is stale",
        )
        winner = next(result[1] for result in results if result[0] == "success")
        assert_only_winner(before, fact_snapshot(a), winner)
    finally:
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
def test_version_comparison_waits_for_lock_and_function_return_retains_lock(
    kind,
    database,
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()
    try:
        transaction = app_connection.begin()
        set_authenticated(app_connection, a)
        app_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(contender, engine, a, pids, kind)
            try:
                pid = pids.get(timeout=10)
                wait_blocked(migrator_connection, [pid])
                winner = call_fact(app_connection, a, kind)
                wait_blocked(migrator_connection, [pid])
                assert not future.done()
            finally:
                transaction.commit()
            assert future.result(timeout=20) == ("40001", "Asset version is stale")
        assert_only_winner(before, fact_snapshot(a), winner)
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("kind", KINDS, ids=lambda kind: kind.name)
def test_installed_functions_use_slice5_lock_before_version_protocol(kind, migrator_connection):
    """Inspect installed SQL as well as the independent behavioral contention proofs."""
    definition = migrator_connection.execute(
        text("""
        SELECT pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops' AND p.proname=:name
    """),
        {"name": kind.function},
    ).scalar_one()
    assert definition.index("current_authenticated_actor()") < definition.index("FOR UPDATE")
    assert definition.index("FOR UPDATE") < definition.index("IF p_expected_version")
    assert definition.index("IF p_expected_version") < definition.index("SELECT h.* INTO latest")
    assert "WHERE a.org_id = trusted_org AND a.id = p_asset_id FOR UPDATE" in definition
