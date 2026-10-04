"""Observe actual PostgreSQL blocking before releasing competing operations."""

import time
from concurrent.futures import ThreadPoolExecutor

from server.tests.auth_context import set_authenticated
from server.tests.slice13.conftest import operation, submit
from sqlalchemy import select, text

from fleetops.api.capture_schemas import CaptureOperation
from fleetops.auth import resolve_identity
from fleetops.capture import service
from fleetops.db import metadata as db


def wait_for_waiter(connection, blocking_pid):
    deadline = time.monotonic() + 10
    while not connection.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_locks l WHERE NOT l.granted "
            "AND :pid=ANY(pg_blocking_pids(l.pid)))"
        ),
        {"pid": blocking_pid},
    ).scalar_one():
        assert time.monotonic() < deadline, "Capture worker never reached PostgreSQL contention"
        time.sleep(0.01)


def test_duplicate_waits_for_first_commit_and_returns_one_effect(
    capture_client, asset_data, app_connection, migrator_connection
):
    tenant = asset_data[0]
    op = operation(tenant)
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    identity = resolve_identity(app_connection, tenant.raw_token)
    pid = app_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    first = service.process(app_connection, identity, CaptureOperation.model_validate(op), None)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(submit, capture_client, tenant, [op])
        try:
            wait_for_waiter(migrator_connection, pid)
            assert not future.done()
            app_connection.commit()
            second = future.result(timeout=15)[0]
        finally:
            app_connection.rollback()
    assert first["sync_state"] == "APPLIED" and second["sync_state"] == "DUPLICATE"
    assert first["result"] == second["result"]
    assert (
        migrator_connection.execute(
            select(db.asset_movements).where(db.asset_movements.c.asset_id == tenant.asset_id)
        ).rowcount
        == 1
    )
    assert migrator_connection.execute(select(db.capture_operations)).rowcount == 1


def test_duplicate_after_first_rollback_applies_once(
    capture_client, asset_data, app_connection, migrator_connection
):
    tenant = asset_data[0]
    op = operation(tenant)
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    pid = app_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    service.process(
        app_connection,
        resolve_identity(app_connection, tenant.raw_token),
        CaptureOperation.model_validate(op),
        None,
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(submit, capture_client, tenant, [op])
        try:
            wait_for_waiter(migrator_connection, pid)
            app_connection.rollback()
            second = future.result(timeout=15)[0]
        finally:
            app_connection.rollback()
    assert second["sync_state"] == "APPLIED"
    assert (
        migrator_connection.execute(
            select(db.asset_movements).where(db.asset_movements.c.asset_id == tenant.asset_id)
        ).rowcount
        == 1
    )
    assert migrator_connection.execute(select(db.capture_operations)).rowcount == 1


def test_capture_competes_with_online_transition_on_shared_asset_lock(
    capture_client, asset_data, app_connection, migrator_connection
):
    from fleetops.domain.assets import transition_asset

    tenant = asset_data[0]
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    app_connection.execute(
        select(db.assets.c.id).where(db.assets.c.id == tenant.asset_id).with_for_update()
    ).scalar_one()
    pid = app_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    queued = operation(tenant)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(submit, capture_client, tenant, [queued])
        try:
            wait_for_waiter(migrator_connection, pid)
            transition_asset(
                app_connection,
                tenant.asset_id,
                values={
                    "expected_version": 1,
                    "from_state": "RECEIVED",
                    "to_state": "IN_STOCK",
                    "reason": "Online winner",
                    "occurred_at": "2026-10-04T12:00:00Z",
                },
            )
            app_connection.commit()
            result = future.result(timeout=15)[0]
        finally:
            app_connection.rollback()
    assert result["sync_state"] == "REJECTED" and result["result"]["code"] == "SYNC_CONFLICT"
    assert result["result"]["current"]["version"] == 2
    assert (
        migrator_connection.execute(
            select(db.asset_movements).where(db.asset_movements.c.asset_id == tenant.asset_id)
        ).all()
        == []
    )
    assert migrator_connection.execute(select(db.sync_conflicts)).rowcount == 1


def test_resolution_contends_without_history_update_grant(
    capture_client, asset_data, app_connection, migrator_connection
):
    from uuid import UUID

    from fleetops.capture import conflicts

    tenant = asset_data[0]
    submit(capture_client, tenant, [operation(tenant)])
    conflict = submit(capture_client, tenant, [operation(tenant)])[0]["result"]["exception_id"]
    app_connection.begin()
    set_authenticated(app_connection, tenant)
    pid = app_connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    conflicts.get_conflict(app_connection, UUID(conflict), lock=True)
    queued = operation(
        tenant,
        operation="RESOLVE",
        entity_type="EXCEPTION",
        entity_id=conflict,
        payload={"expected_status": "OPEN", "note": "Queued resolver"},
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(submit, capture_client, tenant, [queued])
        try:
            wait_for_waiter(migrator_connection, pid)
            conflicts.transition(
                app_connection,
                UUID(conflict),
                values={
                    "expected_status": "OPEN",
                    "to_status": "RESOLVED",
                    "note": "Online resolver",
                    "occurred_at": "1970-01-01T00:00:00Z",
                },
            )
            app_connection.commit()
            result = future.result(timeout=15)[0]
        finally:
            app_connection.rollback()
    assert result["sync_state"] == "REJECTED"
    assert migrator_connection.execute(select(db.sync_conflict_events)).rowcount == 1
