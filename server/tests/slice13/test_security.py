"""Durable runtime permissions, attribution, status succession and rollback boundaries."""

from uuid import UUID, uuid4

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice13.conftest import headers, operation, submit
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from fleetops.db import metadata as db


@pytest.mark.parametrize("table", ["capture_operations", "sync_conflicts", "sync_conflict_events"])
@pytest.mark.parametrize("verb", ["UPDATE", "DELETE", "TRUNCATE"])
def test_runtime_cannot_rewrite_observations(app_connection, asset_data, table, verb):
    with app_connection.begin():
        set_authenticated(app_connection, asset_data[0])
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                command = (
                    f"UPDATE fleetops.{table} SET org_id=org_id"
                    if verb == "UPDATE"
                    else (
                        f"DELETE FROM fleetops.{table}"
                        if verb == "DELETE"
                        else f"TRUNCATE fleetops.{table}"
                    )
                )
                app_connection.exec_driver_sql(command)
        assert caught.value.orig.sqlstate == "42501"


def test_stream_update_cannot_forge_identity_or_decrease_sequence(
    capture_client, app_connection, asset_data
):
    tenant = asset_data[0]
    op = operation(tenant, client_seq=5)
    submit(capture_client, tenant, [op])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError):
            with app_connection.begin_nested():
                app_connection.execute(db.capture_streams.update().values(last_seq=1))
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                app_connection.execute(db.capture_streams.update().values(actor_id=uuid4()))
        assert caught.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("state", ["DUPLICATE", "PENDING_GOVERNANCE"])
def test_reserved_states_cannot_be_inserted(capture_client, app_connection, asset_data, state):
    tenant = asset_data[0]
    op = operation(tenant)
    submit(capture_client, tenant, [op])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        original = dict(app_connection.execute(select(db.capture_operations)).mappings().one())
        for field in ("actor_id", "recorded_at"):
            original.pop(field)
        original.update(operation_id=uuid4(), sync_state=state)
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                app_connection.execute(db.capture_operations.insert().values(original))
        assert caught.value.orig.sqlstate == "23514"


def test_direct_capture_actor_and_clock_are_database_owned(
    capture_client, app_connection, asset_data, migrator_connection
):
    tenant, other = asset_data
    op = operation(tenant)
    submit(capture_client, tenant, [op])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        original = dict(app_connection.execute(select(db.capture_operations)).mappings().one())
        for field in ("actor_id", "recorded_at"):
            original.pop(field)
        forged = dict(original, operation_id=uuid4(), claimed_actor_id=other.actor_id)
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                app_connection.execute(db.capture_operations.insert().values(forged))
        assert caught.value.orig.sqlstate == "23514"
        for field, value in (("actor_id", other.actor_id), ("recorded_at", "1970-01-01T00:00:00Z")):
            with pytest.raises(DBAPIError) as denied:
                with app_connection.begin_nested():
                    app_connection.execute(
                        db.capture_operations.insert().values(
                            dict(original, operation_id=uuid4(), **{field: value})
                        )
                    )
            assert denied.value.orig.sqlstate == "42501"
    assert migrator_connection.execute(select(db.capture_operations)).rowcount == 1


def test_no_credential_or_foreign_scope_cannot_insert_stream(app_connection, asset_data):
    tenant, other = asset_data
    with app_connection.begin():
        app_connection.execute(
            text("SELECT set_config('fleetops.org_id',:org,true)"), {"org": str(tenant.org_id)}
        )
        with pytest.raises(DBAPIError):
            with app_connection.begin_nested():
                app_connection.execute(
                    db.capture_streams.insert().values(
                        org_id=tenant.org_id, client_id=uuid4(), client_epoch=uuid4()
                    )
                )
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError):
            with app_connection.begin_nested():
                app_connection.execute(
                    db.capture_streams.insert().values(
                        org_id=other.org_id, client_id=uuid4(), client_epoch=uuid4()
                    )
                )


def test_revoked_session_cannot_replay_original(capture_client, asset_data, migrator_connection):
    tenant = asset_data[0]
    op = operation(tenant)
    submit(capture_client, tenant, [op])
    response = capture_client.post("/auth/logout", headers=headers(tenant))
    assert response.status_code == 204
    replay = capture_client.post(
        "/capture/operations", headers=headers(tenant), json={"operations": [op]}
    )
    assert replay.status_code == 401
    assert migrator_connection.execute(select(db.capture_operations)).rowcount == 1


def test_conflict_snapshot_forgery_and_missing_capture_origin_reject(
    capture_client, asset_data, app_connection
):
    tenant = asset_data[0]
    submit(capture_client, tenant, [operation(tenant)])
    conflict = submit(capture_client, tenant, [operation(tenant)])[0]["result"]["exception_id"]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        original = dict(
            app_connection.execute(
                select(db.sync_conflicts).where(db.sync_conflicts.c.id == UUID(conflict))
            )
            .mappings()
            .one()
        )
        for field in ("actor_id", "recorded_at"):
            original.pop(field)
        forged = dict(
            original,
            id=uuid4(),
            operation_id=uuid4(),
            current_facts={"version": 2, "state": "READY"},
        )
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                app_connection.execute(db.sync_conflicts.insert().values(forged))
        assert caught.value.orig.sqlstate == "23514"
        with pytest.raises(DBAPIError):
            with app_connection.begin_nested():
                app_connection.execute(
                    db.sync_conflicts.insert().values(
                        dict(original, id=uuid4(), operation_id=uuid4())
                    )
                )
                app_connection.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")


def test_status_rejection_cannot_write_a_second_resolution(
    capture_client, asset_data, app_connection
):
    tenant = asset_data[0]
    submit(capture_client, tenant, [operation(tenant)])
    conflict = submit(capture_client, tenant, [operation(tenant)])[0]["result"]["exception_id"]
    resolve = operation(
        tenant,
        operation="RESOLVE",
        entity_type="EXCEPTION",
        entity_id=conflict,
        payload={"expected_status": "OPEN", "note": "Inspected"},
    )
    assert submit(capture_client, tenant, [resolve])[0]["sync_state"] == "APPLIED"
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        first = dict(app_connection.execute(select(db.sync_conflict_events)).mappings().one())
        first.pop("actor_id")
        first.pop("recorded_at")
        with pytest.raises(DBAPIError) as caught:
            with app_connection.begin_nested():
                app_connection.execute(
                    db.sync_conflict_events.insert().values(
                        dict(first, id=uuid4(), event_seq=2, from_status="OPEN", to_status="WAIVED")
                    )
                )
        assert caught.value.orig.sqlstate == "40001"
