"""Database configuration order, concurrent commits, and independent Asset versions."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import headers
from server.tests.slice5.test_transitions import wait_blocked
from server.tests.slice7.conftest import (
    SEQUENCE,
    call_assignment,
    config_values,
    runtime_configuration,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool

from fleetops.db.metadata import asset_configurations as configs
from fleetops.domain.configurations import current_configuration, list_configurations


def test_configuration_append_uses_database_authority_and_changes_no_asset_fact(
    asset_data,
    app_connection,
    assignment_snapshot,
    asset_client,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    returned = []
    for year in (2099, 1970, 2050):
        row = runtime_configuration(app_connection, a, applied_at=datetime(year, 1, 1, tzinfo=UTC))
        assert row["applied_by"] == a.actor_id
        assert row["id"].version == 7 and row["recorded_at"].tzinfo is not None
        assert row["applied_at"].year == year and row["evidence_ref"] is None
        assert "result_version" not in row
        returned.append(row)
    seqs = [row["configuration_seq"] for row in returned]
    assert seqs == sorted(set(seqs))
    assert assignment_snapshot(a) == before | {configs.name: returned}
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert list_configurations(app_connection, a.asset_id) == returned
        assert current_configuration(app_connection, a.asset_id) == returned[-1]
    history = asset_client.get(f"/assets/{a.asset_id}/configurations", headers=headers(a))
    assert history.status_code == 200
    assert [r["id"] for r in history.json()] == [str(r["id"]) for r in returned]
    assert all("configuration_seq" not in row for row in history.json())
    assert asset_client.get(
        f"/assets/{a.asset_id}/configurations/current", headers=headers(a)
    ).json()["id"] == str(returned[-1]["id"])
    assert asset_client.get("/health/assets/reconciliation", headers=headers(a)).json() == []


def test_no_configuration_is_explicitly_empty(asset_client, asset_data):
    a = asset_data[0]
    assert asset_client.get(f"/assets/{a.asset_id}/configurations", headers=headers(a)).json() == []
    result = asset_client.get(f"/assets/{a.asset_id}/configurations/current", headers=headers(a))
    assert result.status_code == 200 and result.json() is None


def test_concurrent_appends_order_by_allocation_even_when_commits_reverse(
    database,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool, hide_parameters=True)
    allocated, release = Queue(), Queue()

    def append_earlier():
        with engine.begin() as connection:
            set_authenticated(connection, a)
            first = dict(
                connection.execute(configs.insert().values(config_values(a)).returning(configs))
                .mappings()
                .one()
            )
            allocated.put(first)
            release.get(timeout=20)
        return first

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(append_earlier)
            first = allocated.get(timeout=10)
            try:
                second = runtime_configuration(app_connection, a)
                assert first["configuration_seq"] < second["configuration_seq"]
                with app_connection.begin():
                    set_authenticated(app_connection, a)
                    assert list_configurations(app_connection, a.asset_id) == [second]
                    assert current_configuration(app_connection, a.asset_id) == second
            finally:
                release.put(True)
            assert pending.result(timeout=20) == first
        with app_connection.begin():
            set_authenticated(app_connection, a)
            assert list_configurations(app_connection, a.asset_id) == [first, second]
            assert current_configuration(app_connection, a.asset_id) == second
        assert assignment_snapshot(a) == before | {configs.name: [first, second]}
    finally:
        engine.dispose()


def test_aborted_allocation_leaves_gap_without_history_or_asset_version(
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with app_connection.begin() as transaction:
        set_authenticated(app_connection, a)
        abandoned = dict(
            app_connection.execute(configs.insert().values(config_values(a)).returning(configs))
            .mappings()
            .one()
        )
        transaction.rollback()
    assert assignment_snapshot(a) == before
    committed = runtime_configuration(app_connection, a)
    assert committed["configuration_seq"] > abandoned["configuration_seq"]
    assert assignment_snapshot(a) == before | {configs.name: [committed]}


def test_configuration_and_assignment_can_commit_independently(
    database,
    asset_data,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool, hide_parameters=True)
    try:
        with app_connection.begin() as transaction:
            set_authenticated(app_connection, a)
            configuration = dict(
                app_connection.execute(configs.insert().values(config_values(a)).returning(configs))
                .mappings()
                .one()
            )
            # Observe the real FK KEY SHARE interaction without introducing a version
            # precondition on configurations. Both operations must still commit.
            pids = Queue()
            with ThreadPoolExecutor(max_workers=1) as executor:

                def assign():
                    with engine.begin() as connection:
                        connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                        set_authenticated(connection, a)
                        pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                        return call_assignment(connection, a)

                future = executor.submit(assign)
                try:
                    wait_blocked(migrator_connection, [pids.get(timeout=10)])
                finally:
                    transaction.commit()
                assigned = future.result(timeout=20)
        after = assignment_snapshot(a)
        assert after["asset"]["version"] == 2
        assert after["asset_assignment_events"] == [assigned]
        assert after[configs.name] == [configuration]
        assert after["asset_transitions"] == before["asset_transitions"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "column,value",
    [
        ("configuration_seq", 1),
        ("recorded_at", datetime(1970, 1, 1, tzinfo=UTC)),
        ("applied_by", None),
    ],
)
def test_raw_runtime_insert_cannot_choose_database_authority(
    column,
    value,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_configuration(app_connection, a, **{column: value})
    assert error.value.orig.sqlstate in {"42501", "428C9"}
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize(
    "operation",
    [
        f"SELECT nextval('{SEQUENCE}')",
        f"SELECT setval('{SEQUENCE}', 1)",
        f"ALTER SEQUENCE {SEQUENCE} RESTART WITH 1",
    ],
)
def test_runtime_cannot_allocate_or_reset_configuration_sequence_directly(
    operation,
    app_connection,
    assert_denied,
):
    assert_denied(app_connection, operation)


def test_overriding_identity_cannot_bypass_column_privileges(
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error, app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.execute(
            text("""
            INSERT INTO fleetops.asset_configurations
                (id, org_id, asset_id, image_name, image_version, config_profile, notes,
                 applied_at, configuration_seq)
            OVERRIDING SYSTEM VALUE VALUES
                (:id, :org_id, :asset_id, :image_name, :image_version, :config_profile, :notes,
                 :applied_at, 1)
        """),
            config_values(a),
        )
    assert error.value.orig.sqlstate == "42501"
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"evidence_ref": "019a0000-0000-7000-8000-000000000001"}, "23514"),
        ({"applied_at": None}, "23502"),
        ({"image_name": "\t\n"}, "23514"),
        ({"image_version": "x" * 201}, "23514"),
        ({"notes": "x" * 4001}, "23514"),
    ],
)
def test_invalid_configuration_rolls_back_all_row_and_asset_effects(
    changes,
    code,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_configuration(app_connection, a, **changes)
    assert error.value.orig.sqlstate == code
    assert assignment_snapshot(a) == before


def test_equal_recording_times_and_reverse_uuid_order_cannot_select_current(
    asset_data,
    app_connection,
    migrator_connection,
):
    a = asset_data[0]
    first = runtime_configuration(app_connection, a)
    second = runtime_configuration(app_connection, a)
    # Deliberate administrative timestamp/identity corruption isolates the query's order.
    same = datetime(2000, 1, 1, tzinfo=UTC)
    migrator_connection.execute(
        configs.update().where(configs.c.asset_id == a.asset_id).values(recorded_at=same)
    )
    migrator_connection.execute(
        configs.update()
        .where(configs.c.id == first["id"])
        .values(id="ffffffff-ffff-7fff-bfff-ffffffffffff")
    )
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        result = current_configuration(app_connection, a.asset_id)
        assert result["id"] == second["id"]
