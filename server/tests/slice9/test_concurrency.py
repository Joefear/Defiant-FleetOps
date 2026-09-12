"""Real blocked PostgreSQL transactions prove retained comparator and canonical uniqueness locks."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep
from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    UNIT_CALL,
    exception_values,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
    unit_parameters,
)
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from uuid6 import uuid7

from fleetops.api.receiving_schemas import ReceiptCreate, ReceiptLineCreate
from fleetops.db.metadata import purchase_order_lines, purchase_orders, receiving_exceptions
from fleetops.domain import procurement, receiving


def blocked(observer, pid):
    """Observe PostgreSQL's actual blocking graph rather than rely on scheduling delays."""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if observer.execute(
            text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
        ).scalar_one():
            return
        sleep(0.01)
    pytest.fail("Receiving contender never reached the expected PostgreSQL lock")


def worker(engine, tenant, pids, action):
    """Each contender owns an independent real app login and commits only after all writes."""
    try:
        with engine.connect() as connection, connection.begin():
            connection.exec_driver_sql("SET LOCAL statement_timeout='15s'")
            set_authenticated(connection, tenant)
            pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
            result = action(connection)
        return "committed", result
    except DBAPIError as error:
        return error.orig.sqlstate, None
    except (receiving.ReceivingConflict, procurement.ProcurementConflict):
        return "conflict", None


def supersede(connection, tenant, po, original):
    return dict(
        procurement.create_line(
            connection,
            po["id"],
            performer_id=tenant.actor_id,
            predecessor_id=original["id"],
            values={
                "item_id": original["item_id"],
                "quantity": 7,
                "unit_price": "321.123456789",
                "expected_date": None,
            },
        )
    )


@pytest.mark.parametrize("first", ["supersession", "receipt"])
def test_receiving_and_supersession_share_po_lock_in_both_commit_orders(
    first,
    receiving_client,
    space_data,
    make_item,
    make_order,
    database,
    app_connection,
    migrator_connection,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 1}])
    original = expected[0]
    before = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
    payload = ReceiptCreate.model_validate(
        receipt_data(
            a,
            po_id=po["id"],
            lines=[line_data(a, item_id=item, unit=None, po_line_id=original["id"])],
        )
    ).model_dump()

    def receive(c):
        return receiving.create_receipt(c, org_id=a.org_id, values=payload)

    def amend(c):
        return supersede(c, a, po, original)

    first_action, second_action = (amend, receive) if first == "supersession" else (receive, amend)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        first_result = first_action(app_connection)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            future = pool.submit(worker, engine, a, pids, second_action)
            try:
                blocked(migrator_connection, pids.get(timeout=10))
            finally:
                app_connection.commit()
                migrator_connection.rollback()
            status, _ = future.result(timeout=20)
        assert status == ("conflict" if first == "supersession" else "committed")
        response = receiving_client.get("/receipts", headers=headers(a))
        records = response.json()
        if first == "supersession":
            assert records == []
        else:
            assert len(records) == 1 and records[0]["id"] == str(first_result["id"])
            assert records[0]["lines"][0]["po_line_id"] == str(original["id"])
            assert records[0]["exceptions"] == []
        after = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
        assert after["purchase_orders"] == before["purchase_orders"]
        assert before["purchase_order_lines"][0] in after["purchase_order_lines"]
        assert len(after["purchase_order_lines"]) == 2
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


def test_concurrent_normal_serial_claim_loser_rolls_back_then_fresh_retry_is_known_conflict(
    receiving_client, space_data, database, app_connection, migrator_connection
):
    a = space_data[0]
    receipts = [
        post_receipt(receiving_client, a, receipt_data(a, reconcile=False)) for _ in range(2)
    ]
    serial = str(uuid7())
    params = [unit_parameters(a, UUID(row["id"]), identifier_value=serial) for row in receipts]

    def normal(connection, values):
        result = dict(connection.execute(text(UNIT_CALL), values).mappings().one())
        connection.execute(
            receiving_exceptions.insert().values(
                exception_values(
                    a,
                    values["receipt_id"],
                    "UNEXPECTED_ITEM",
                    receipt_line_id=values["line_id"],
                    asset_id=values["asset_id"],
                )
            )
        )
        return result

    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        normal(app_connection, params[0])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            future = pool.submit(worker, engine, a, pids, lambda c: normal(c, params[1]))
            try:
                blocked(migrator_connection, pids.get(timeout=10))
            finally:
                app_connection.commit()
                migrator_connection.rollback()
            status, _ = future.result(timeout=20)
        assert status == "23505"
        before_retry = snapshot(migrator_connection, a.org_id)
        assert len(before_retry["assets"]) == len(before_retry["asset_identifiers"]) == 1
        assert len(before_retry["asset_transitions"]) == 1
        assert len(before_retry["asset_initial_facts"]) == 1
        assert len(before_retry["asset_initial_assignment_facts"]) == 1
        assert len(before_retry["receipt_lines"]) == len(before_retry["receiving_exceptions"]) == 1
        assert (
            receiving_client.get(f"/receipts/{receipts[1]['id']}", headers=headers(a)).json()[
                "lines"
            ]
            == []
        )
        fresh = line_data(a)
        fresh["unit"]["identifier"]["value"] = serial
        response = receiving_client.post(
            f"/receipts/{receipts[1]['id']}/lines", headers=headers(a), json=json_data(fresh)
        )
        assert response.status_code == 201, response.text
        retried = response.json()
        assert sorted(e["exception_type"] for e in retried["exceptions"]) == [
            "SERIAL_MISMATCH",
            "UNEXPECTED_ITEM",
        ]
        assert retried["lines"][0]["asset_id"] is None
        mismatch = next(
            e for e in retried["exceptions"] if e["exception_type"] == "SERIAL_MISMATCH"
        )
        assert mismatch["conflicting_asset_id"] == str(params[0]["asset_id"])
        after_retry = snapshot(migrator_connection, a.org_id)
        for table in ASSET_TABLES:
            assert after_retry[table.name] == before_retry[table.name]
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("first", ["append", "reconcile"])
def test_receipt_population_seal_serializes_concurrent_append(
    first,
    receiving_client,
    space_data,
    make_item,
    make_order,
    database,
    app_connection,
    migrator_connection,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 2}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"])],
            reconcile=False,
        ),
    )
    receipt_id = UUID(result["id"])
    values = ReceiptLineCreate.model_validate(
        line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"])
    ).model_dump()

    def append(c):
        return receiving.add_line(c, receipt_id, values=values)

    def seal(c):
        return receiving.reconcile_receipt(c, receipt_id)

    first_action, second_action = (append, seal) if first == "append" else (seal, append)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        first_action(app_connection)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            future = pool.submit(worker, engine, a, pids, second_action)
            try:
                blocked(migrator_connection, pids.get(timeout=10))
            finally:
                app_connection.commit()
                migrator_connection.rollback()
            status, _ = future.result(timeout=20)
        assert status == ("committed" if first == "append" else "conflict")
        stored = receiving_client.get(f"/receipts/{receipt_id}", headers=headers(a)).json()
        assert stored["reconciled"] is True
        assert len(stored["lines"]) == (2 if first == "append" else 1)
        assert [e["exception_type"] for e in stored["exceptions"]] == (
            [] if first == "append" else ["SHORT"]
        )
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("isolation", ["REPEATABLE READ", "SERIALIZABLE"])
def test_receiving_rejects_stale_snapshot_isolation_before_any_new_fact(
    isolation, receiving_client, space_data, database, migrator_connection
):
    a = space_data[0]
    before = snapshot(migrator_connection, a.org_id)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        with engine.connect().execution_options(isolation_level=isolation) as connection:
            with pytest.raises(receiving.ReceivingConflict):
                with connection.begin():
                    set_authenticated(connection, a)
                    receiving.create_receipt(
                        connection,
                        org_id=a.org_id,
                        values=ReceiptCreate.model_validate(receipt_data(a)).model_dump(),
                    )
        assert snapshot(migrator_connection, a.org_id) == before
    finally:
        engine.dispose()
