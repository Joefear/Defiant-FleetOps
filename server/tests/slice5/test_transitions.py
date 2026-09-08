"History authority, global-version gaps and actual competing PostgreSQL transactions."

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue
from time import monotonic, sleep

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import call_transition, runtime_transition
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from uuid6 import uuid7

from fleetops.auth import token_digest
from fleetops.db.metadata import actors, asset_transitions, assets
from fleetops.db.tenancy import set_credential_context
from fleetops.domain.assets import reconcile_state, transition_asset
from fleetops.domain.lifecycle import AssetState, LifecycleInvalid


def test_success_is_one_atomic_global_increment_and_preserves_orthogonal_facts(
    asset_data, app_connection, snapshot
):
    a, _ = asset_data
    before, _ = snapshot(a)
    result = runtime_transition(app_connection, a)
    after, history = snapshot(a)
    assert result["id"].version == 7
    assert len(history) == 2
    assert result == history[-1]
    assert result["result_version"] == after["version"] == 2
    assert result["to_state"] == after["current_state"] == "IN_STOCK"
    assert (
        result["evidence_ref"] is result["client_op_id"] is result["corrects_transition_id"] is None
    )
    assert {
        key: value for key, value in after.items() if key not in {"version", "current_state"}
    } == {key: value for key, value in before.items() if key not in {"version", "current_state"}}


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expected_version": 0}, "40001"),
        ({"expected_version": None}, "40001"),
        ({"from_state": "READY"}, "P0001"),
        ({"from_state": None}, "P0001"),
        ({"evidence_ref": uuid7()}, "23514"),
        ({"reason": ""}, "23514"),
        ({"occurred_at": None}, "23502"),
        ({"to_state": "ORDERED"}, "23514"),
        ({"transition_id": None}, "23502"),
    ],
)
def test_failed_transition_has_no_partial_history_or_projection(
    changes, code, asset_data, app_connection, snapshot
):
    a, _ = asset_data
    before = snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_transition(app_connection, a, **changes)
    assert error.value.orig.sqlstate == code
    assert snapshot(a) == before


@pytest.mark.parametrize("kind", ["state", "version_direction", "missing"])
def test_corruption_reconciles_and_rejects_without_repair(
    kind, asset_data, app_connection, migrator_connection, snapshot
):
    a, _ = asset_data
    if kind == "state":
        migrator_connection.execute(
            assets.update().where(assets.c.id == a.asset_id).values(current_state="READY")
        )
    elif kind == "version_direction":
        migrator_connection.execute(
            asset_transitions.insert().values(
                id=uuid7(),
                org_id=a.org_id,
                asset_id=a.asset_id,
                result_version=2,
                from_state="RECEIVED",
                to_state="RECEIVED",
                reason="Privileged corruption probe",
                actor_id=a.actor_id,
                occurred_at=datetime.now(UTC),
            )
        )
    else:
        migrator_connection.execute(
            asset_transitions.delete().where(asset_transitions.c.asset_id == a.asset_id)
        )
    migrator_connection.commit()
    before = snapshot(a)
    with app_connection.begin():
        set_authenticated(app_connection, a)
        discrepancies = reconcile_state(app_connection)
    assert len(discrepancies) == 1
    assert discrepancies[0]["asset_id"] == a.asset_id
    assert discrepancies[0]["discrepancies"] == [
        {
            "state": "state_mismatch",
            "version_direction": "history_version_ahead",
            "missing": "missing_history",
        }[kind]
    ]
    with pytest.raises(DBAPIError) as error:
        runtime_transition(app_connection, a)
    assert error.value.orig.sqlstate == "P0001"
    assert snapshot(a) == before


def test_global_version_gap_is_valid_for_admission_and_state_reconciliation(
    asset_data, app_connection, migrator_connection, snapshot
):
    a, _ = asset_data
    # Simulate a future orthogonal version producer with privileged setup only.
    migrator_connection.execute(assets.update().where(assets.c.id == a.asset_id).values(version=7))
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert reconcile_state(app_connection) == []
    result = runtime_transition(app_connection, a, expected_version=7)
    row, history = snapshot(a)
    assert result["result_version"] == row["version"] == 8
    assert [event["result_version"] for event in history] == [1, 8]


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_context",
        "other_asset",
        "missing_asset",
        "other_actor",
        "missing_actor",
        "inactive_actor",
    ],
)
def test_definer_checks_tenant_and_active_actor_explicitly(
    invalid, asset_data, app_connection, migrator_connection, snapshot
):
    a, b = asset_data
    if invalid == "inactive_actor":
        migrator_connection.execute(
            actors.update().where(actors.c.id == a.actor_id).values(active=False)
        )
        migrator_connection.commit()
    before = snapshot(a), snapshot(b)
    changes = {
        "other_asset": {"asset_id": b.asset_id},
        "missing_asset": {"asset_id": uuid7()},
    }.get(invalid, {})
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if invalid != "missing_context":
                set_authenticated(app_connection, a)
                if invalid == "other_actor":
                    set_credential_context(app_connection, token_digest(b.raw_token))
                elif invalid == "missing_actor":
                    set_credential_context(app_connection, token_digest("unissued"))
            call_transition(app_connection, a, **changes)
    expected = "P0002" if invalid in {"other_asset", "missing_asset"} else "42501"
    assert error.value.orig.sqlstate == expected
    if expected == "P0002":
        assert error.value.orig.diag.message_primary == "Asset not found"
    assert (snapshot(a), snapshot(b)) == before


def wait_blocked(observer, pids):
    "Require actual server-observed lock waits; sleeps alone cannot prove concurrency."
    deadline = monotonic() + 10
    while monotonic() < deadline:
        blocked = all(
            observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            ).scalar_one()
            for pid in pids
        )
        if blocked:
            return
        sleep(0.01)
    pytest.fail("Transition did not block on the held Asset row lock")


def contender(engine, tenant, pids, to_state):
    "One independent runtime login/transaction, returning only a committed result or SQLSTATE."
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
            assert connection.exec_driver_sql("SELECT current_user").scalar_one() == "fleetops_app"
            set_authenticated(connection, tenant)
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            row = call_transition(connection, tenant, to_state=to_state)
        return "success", row
    except DBAPIError as error:
        return error.orig.sqlstate, error.orig.diag.message_primary


def test_concurrent_same_version_has_exactly_one_winner(
    database, asset_data, migrator_connection, snapshot
):
    a, _ = asset_data
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()
    try:
        migrator_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(contender, engine, a, pids, state)
                for state in ("IN_STOCK", "ON_HOLD")
            ]
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10), pids.get(timeout=10)])
            finally:
                migrator_connection.commit()
            results = [future.result(timeout=20) for future in futures]
        assert sorted(result[0] for result in results) == ["40001", "success"]
        winner = next(result[1] for result in results if result[0] == "success")
        row, history = snapshot(a)
        assert row["version"] == 2
        assert row["current_state"] == winner["to_state"]
        assert len(history) == 2 and history[-1] == winner
    finally:
        migrator_connection.rollback()
        engine.dispose()


def test_version_is_revalidated_after_waiting_for_asset_lock(
    database, asset_data, app_connection, migrator_connection, snapshot
):
    a, _ = asset_data
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()
    try:
        transaction = app_connection.begin()
        set_authenticated(app_connection, a)
        app_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(contender, engine, a, pids, "ON_HOLD")
            try:
                pid = pids.get(timeout=10)
                wait_blocked(migrator_connection, [pid])
                # B has started against version 1 and is blocked. A advances through
                # the authorized function, so a pre-lock-only comparison would admit B.
                call_transition(app_connection, a)
                wait_blocked(
                    migrator_connection, [pid]
                )  # Function return does not release the lock.
            finally:
                transaction.commit()
            result = future.result(timeout=20)
        assert result == ("40001", "Asset version is stale")
        row, history = snapshot(a)
        assert row["version"] == 2 and len(history) == 2
        assert row["current_state"] == "IN_STOCK"
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


def test_client_operation_id_is_correlation_without_idempotency(
    asset_data, app_connection, snapshot
):
    a, _ = asset_data
    correlation = uuid7()
    runtime_transition(app_connection, a, client_op_id=correlation)
    runtime_transition(
        app_connection,
        a,
        expected_version=2,
        from_state="IN_STOCK",
        to_state="CONFIGURING",
        client_op_id=correlation,
    )
    row, history = snapshot(a)
    assert row["version"] == 3 and len(history) == 3
    assert history[1]["client_op_id"] == history[2]["client_op_id"] == correlation


def test_on_hold_admission_does_not_use_history_from_before_lock_acquisition(
    database, asset_data, app_connection, migrator_connection, snapshot
):
    a, _ = asset_data
    current = "RECEIVED"
    for version, target in enumerate(("IN_STOCK", "CONFIGURING", "READY", "ON_HOLD"), 1):
        runtime_transition(
            app_connection, a, expected_version=version, from_state=current, to_state=target
        )
        current = target
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()

    def requested_exit():
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                set_authenticated(connection, a)
                pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                transition_asset(
                    connection,
                    a.asset_id,
                    values=dict(
                        expected_version=8,
                        from_state=AssetState.ON_HOLD,
                        to_state=AssetState.READY,
                        reason="Predicted future version",
                        occurred_at=datetime.now(UTC),
                    ),
                )
            return "admitted"
        except LifecycleInvalid:
            return "illegal"

    try:
        transaction = app_connection.begin()
        set_authenticated(app_connection, a)
        app_connection.execute(
            select(assets.c.id).where(assets.c.id == a.asset_id).with_for_update()
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(requested_exit)
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10)])
                # The old ON_HOLD entered from READY; the new one enters from CONFIGURING.
                # Guessing the later version must not bind an exit to the earlier history.
                for version, source, target in (
                    (5, "ON_HOLD", "OUT_OF_SERVICE"),
                    (6, "OUT_OF_SERVICE", "CONFIGURING"),
                    (7, "CONFIGURING", "ON_HOLD"),
                ):
                    call_transition(
                        app_connection,
                        a,
                        expected_version=version,
                        from_state=source,
                        to_state=target,
                    )
            finally:
                transaction.commit()
            assert future.result(timeout=20) == "illegal"
        row, history = snapshot(a)
        assert row["version"] == len(history) == 8
        assert row["current_state"] == "ON_HOLD"
        assert history[-1]["from_state"] == "CONFIGURING"
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()
