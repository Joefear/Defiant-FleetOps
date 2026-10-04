"""Real competing runtime transactions share governing locks and post-lock tokens."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Barrier
from time import monotonic, sleep

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import ASSET_TABLES, WHEN, line_data, receipt_data
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from fleetops.db.metadata import (
    assets,
    exception_workflows,
    receipt_evaluation_exceptions,
    receiving_exceptions,
)
from fleetops.domain import (
    asset_facts,
    assignments,
    corrections,
    exception_workflow,
    receiving,
    record_corrections,
)
from fleetops.domain import assets as asset_service
from fleetops.domain.assets import AssetConflict


def wait_for_holder(observer, worker_pid, holder_pid):
    """Observe this exact holder in PostgreSQL's lock graph, not a scheduled thread."""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        blocked = observer.execute(
            text("SELECT :holder = ANY(pg_blocking_pids(:worker))"),
            dict(holder=holder_pid, worker=worker_pid),
        ).scalar_one()
        observer.rollback()
        if blocked:
            return
        sleep(0.01)
    pytest.fail("Worker did not block on the retained row lock")


def test_concurrent_asset_corrections_have_one_global_winner(database, asset_data, app_connection):
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = asset_facts.move_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                to_location_id=tenant.other_location_id,
                reason="Original move",
                occurred_at=WHEN,
            ),
        )
    barrier = Barrier(2)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)

    def attempt(destination):
        try:
            with engine.begin() as connection:
                set_authenticated(connection, tenant)
                barrier.wait(timeout=10)
                corrections.correct_movement(
                    connection,
                    tenant.asset_id,
                    root["id"],
                    values=dict(
                        expected_version=2,
                        to_location_id=destination,
                        reason="Concurrent correction",
                        correction_occurred_at=WHEN,
                    ),
                )
            return "accepted"
        except AssetConflict:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(attempt, [None, tenant.location_id]))
        assert sorted(outcomes) == ["accepted", "stale"]
    finally:
        engine.dispose()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert (
            app_connection.execute(
                select(assets.c.version).where(assets.c.id == tenant.asset_id)
            ).scalar_one()
            == 4
        )
        assert len(asset_facts.list_movements(app_connection, tenant.asset_id)) == 3
        assert not asset_facts.reconcile_assets(app_connection)


@pytest.mark.parametrize("kind", ["procurement", "receipt"])
def test_nonversion_corrections_allocate_one_generation(
    kind,
    database,
    space_data,
    make_item,
    make_order,
    app_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    if kind == "receipt":
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            captured = receiving.create_receipt(
                app_connection,
                org_id=tenant.org_id,
                values=receipt_data(
                    tenant,
                    po_id=po["id"],
                    lines=[line_data(tenant, item_id=item, po_line_id=lines[0]["id"], unit=None)],
                ),
            )
    barrier = Barrier(2)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)

    def attempt(quantity):
        try:
            with engine.begin() as connection:
                set_authenticated(connection, tenant)
                barrier.wait(timeout=10)
                values = dict(
                    expected_generation=0,
                    quantity=quantity,
                    reason="Concurrent fact correction",
                    correction_occurred_at=WHEN,
                )
                if kind == "procurement":
                    record_corrections.correct_procurement(
                        connection, po["id"], lines[0]["id"], values=values
                    )
                else:
                    record_corrections.correct_receipt_line(
                        connection, captured["id"], captured["lines"][0]["id"], values=values
                    )
            return "accepted"
        except AssetConflict:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            outcomes = list(workers.map(attempt, [2, 3]))
        assert sorted(outcomes) == ["accepted", "stale"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("terminal", ["RESOLVED", "WAIVED"])
def test_receipt_correction_waits_for_terminal_workflow_without_veto(
    terminal,
    database,
    space_data,
    make_item,
    make_order,
    app_connection,
    migrator_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        captured = receiving.create_receipt(
            app_connection,
            org_id=tenant.org_id,
            values=receipt_data(
                tenant,
                po_id=po["id"],
                lines=[
                    line_data(
                        tenant,
                        item_id=item,
                        po_line_id=lines[0]["id"],
                        unit=None,
                        condition="DAMAGED",
                    )
                ],
            ),
        )
        observation = dict(captured["exceptions"][0])
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()

    def correct():
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL statement_timeout='20s'")
            set_authenticated(connection, tenant)
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            return record_corrections.correct_receipt_line(
                connection,
                captured["id"],
                captured["lines"][0]["id"],
                values=dict(
                    expected_generation=0,
                    condition="GOOD",
                    reason="Inspected again",
                    correction_occurred_at=WHEN,
                ),
            )

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            # Roll back the holder before joining the worker if any assertion fails.
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                holder = app_connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                app_connection.execute(
                    select(exception_workflows)
                    .where(exception_workflows.c.exception_id == observation["id"])
                    .with_for_update()
                )
                future = workers.submit(correct)
                worker = pids.get(timeout=10)
                wait_for_holder(migrator_connection, worker, holder)
                assert not future.done()
                event = dict(
                    exception_workflow.transition_exception(
                        app_connection,
                        observation["id"],
                        values=dict(
                            expected_status="OPEN",
                            to_status=terminal,
                            note="Operator terminal disposition",
                            occurred_at=WHEN,
                        ),
                    )
                )
                terminal_state = exception_workflow.get_exception(app_connection, observation["id"])
                # Returning from the transition must not release its database lock.
                wait_for_holder(migrator_connection, worker, holder)
                assert not future.done()
            assert future.result(timeout=20)["correction_generation"] == 1
    finally:
        engine.dispose()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        result = exception_workflow.get_exception(app_connection, observation["id"])
        assert result == terminal_state
        assert result["status"] == terminal
        assert result["event_seq"] == 1
        assert [dict(row) for row in result["events"]] == [event]
        assert (
            dict(
                app_connection.execute(
                    select(receiving_exceptions).where(
                        receiving_exceptions.c.id == observation["id"]
                    )
                )
                .mappings()
                .one()
            )
            == observation
        )
        consequence = (
            app_connection.execute(
                select(receipt_evaluation_exceptions).where(
                    receipt_evaluation_exceptions.c.exception_id == observation["id"]
                )
            )
            .mappings()
            .one()
        )
        assert consequence["supported"] is False
        assert consequence["prior_event_seq"] == 1


@pytest.mark.parametrize("winner", ["correction", "ordinary"])
@pytest.mark.parametrize("kind", ["movement", "custody", "ownership", "assignment", "lifecycle"])
def test_correction_contends_with_ordinary_global_version(
    winner,
    kind,
    database,
    asset_data,
    app_connection,
    migrator_connection,
):
    """Both orderings share N=2; a blocked loser must leave no partial pair or projection."""
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = dict(
            asset_facts.move_asset(
                app_connection,
                tenant.asset_id,
                values=dict(
                    expected_version=1,
                    to_location_id=tenant.other_location_id,
                    reason="Original move",
                    occurred_at=WHEN,
                ),
            )
        )

    def operation(connection, choice):
        common = dict(expected_version=2, reason="Contending operation", occurred_at=WHEN)
        if choice == "correction":
            return corrections.correct_movement(
                connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    expected_version=2,
                    to_location_id=None,
                    reason="Correct original move",
                    correction_occurred_at=WHEN,
                ),
            )
        if kind == "movement":
            return asset_facts.move_asset(
                connection,
                tenant.asset_id,
                values=dict(common, to_location_id=tenant.location_id),
            )
        if kind == "custody":
            return asset_facts.change_custody(
                connection,
                tenant.asset_id,
                values=dict(common, to_custodian_party_id=None),
            )
        if kind == "ownership":
            return asset_facts.change_ownership(
                connection,
                tenant.asset_id,
                values=dict(common, to_owner_party_id=tenant.party_id),
            )
        if kind == "assignment":
            return assignments.assign_asset(
                connection,
                tenant.asset_id,
                values=dict(common, assignee_type="ACTOR", assignee_id=tenant.actor_id),
            )
        return asset_service.transition_asset(
            connection,
            tenant.asset_id,
            values=dict(common, from_state="RECEIVED", to_state="IN_STOCK"),
        )

    def all_rows(connection):
        # Compare every column, including all raw correction members and mutable projections.
        return {
            table.name: connection.execute(
                select(table)
                .where(table.c.org_id == tenant.org_id)
                .order_by(*table.primary_key.columns)
            ).all()
            for table in ASSET_TABLES
        }

    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()

    def losing_attempt():
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("SET LOCAL statement_timeout='20s'")
                set_authenticated(connection, tenant)
                pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                operation(connection, "ordinary" if winner == "correction" else "correction")
            return "accepted"
        except AssetConflict:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                holder = app_connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
                app_connection.execute(
                    select(assets).where(assets.c.id == tenant.asset_id).with_for_update()
                )
                future = workers.submit(losing_attempt)
                worker = pids.get(timeout=10)
                wait_for_holder(migrator_connection, worker, holder)
                produced = operation(app_connection, winner)
                assert produced["result_version"] == (4 if winner == "correction" else 3)
                expected = all_rows(app_connection)
                wait_for_holder(migrator_connection, worker, holder)
                assert not future.done()
            assert future.result(timeout=20) == "stale"
    finally:
        engine.dispose()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert all_rows(app_connection) == expected
        raw = [
            row._mapping
            for table, rows in expected.items()
            for row in rows
            if table
            in (
                "asset_transitions",
                "asset_movements",
                "asset_custody_changes",
                "asset_ownership_changes",
                "asset_assignment_events",
            )
        ]
        version = 4 if winner == "correction" else 3
        assert sorted(row["result_version"] for row in raw) == list(range(1, version + 1))
        assert sum(row["correction_role"] != "NONE" for row in raw) == (
            2 if winner == "correction" else 0
        )
        assert next(dict(row) for row in raw if row["id"] == root["id"]) == root
        assert (
            app_connection.execute(
                select(assets.c.version).where(assets.c.id == tenant.asset_id)
            ).scalar_one()
            == version
        )
        assert not asset_facts.reconcile_assets(app_connection)
