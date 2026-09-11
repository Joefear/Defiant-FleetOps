"""Independent runtime logins exercise database guards without Python service coordination."""

from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from time import monotonic, sleep

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice8.conftest import line_values, snapshot, write
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.pool import NullPool
from uuid6 import uuid7

from fleetops.db.metadata import items
from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders
from fleetops.domain.procurement import list_lines


def wait_blocked(observer, pids):
    """Prove contenders reached conflicting PostgreSQL locks, independent of scheduling."""
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if all(
            observer.execute(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": pid}
            ).scalar_one()
            for pid in pids
        ):
            return
        sleep(0.01)
    pytest.fail("Procurement contender did not reach the expected PostgreSQL lock")


def contender(engine, tenant, pids, statement, isolation="READ COMMITTED"):
    """A result is successful only after its independent transaction commits."""
    try:
        with engine.connect().execution_options(isolation_level=isolation) as connection:
            with connection.begin():
                connection.exec_driver_sql("SET LOCAL statement_timeout = '15s'")
                set_authenticated(connection, tenant)
                pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                rows = connection.execute(statement).mappings().all()
            return "success", rows
    except DBAPIError as error:
        return error.orig.sqlstate, []


@pytest.mark.parametrize("kind", ["root", "successor"])
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
def test_competing_roots_and_successors_have_one_winner_and_no_partial_loser(
    kind,
    isolation,
    draft,
    space_data,
    database,
    app_connection,
    migrator_connection,
):
    a, (po, original) = space_data[0], draft
    if kind == "successor":
        write(
            app_connection,
            a,
            orders.update()
            .where(orders.c.id == po["id"])
            .values(status="ISSUED")
            .returning(orders),
        )
    before = snapshot(migrator_connection, lines, original["id"])
    values = [
        line_values(a, po["id"], line_number=2)
        if kind == "root"
        else line_values(a, po["id"], supersedes_line_id=original["id"])
        for _ in range(2)
    ]
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        migrator_connection.execute(
            select(orders).where(orders.c.id == po["id"]).with_for_update(key_share=True)
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            pids = Queue()
            futures = [
                pool.submit(
                    contender, engine, a, pids, lines.insert().values(v).returning(lines), isolation
                )
                for v in values
            ]
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10) for _ in futures])
            finally:
                migrator_connection.commit()
            results = [f.result(timeout=20) for f in futures]
        assert [status for status, _ in results].count("success") == 1
        assert all(status in {"success", "23505", "40001"} for status, _ in results)
        with app_connection.begin():
            set_authenticated(app_connection, a)
            history = list_lines(app_connection, po["id"])
        assert len(history) == 2
        assert sum(r["active"] for r in history) == (2 if kind == "root" else 1)
        assert sum(r["id"] in [v["id"] for v in values] for r in history) == 1
        assert snapshot(migrator_connection, lines, original["id"]) == before
    finally:
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("operation", ["edit", "create"])
@pytest.mark.parametrize("first", ["issue", "draft_write"])
def test_issuance_serializes_draft_edit_and_creation_in_both_commit_orders(
    operation,
    first,
    draft,
    space_data,
    database,
    app_connection,
    migrator_connection,
):
    a, (po, original) = space_data[0], draft
    issue = orders.update().where(orders.c.id == po["id"]).values(status="ISSUED").returning(orders)
    change = (
        lines.update().where(lines.c.id == original["id"]).values(quantity=7).returning(lines)
        if operation == "edit"
        else lines.insert().values(line_values(a, po["id"], line_number=2)).returning(lines)
    )
    first_statement, second_statement = (issue, change) if first == "issue" else (change, issue)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        app_connection.execute(first_statement).all()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            future = pool.submit(contender, engine, a, pids, second_statement)
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10)])
            finally:
                app_connection.commit()
                migrator_connection.rollback()
            result, _ = future.result(timeout=20)
        assert result == ("23514" if first == "issue" else "success")
        assert snapshot(migrator_connection, orders, po["id"])["status"] == "ISSUED"
        with app_connection.begin():
            set_authenticated(app_connection, a)
            history = list_lines(app_connection, po["id"])
        assert len(history) == (2 if operation == "create" and first == "draft_write" else 1)
        assert history[0]["quantity"] == (
            7 if operation == "edit" and first == "draft_write" else original["quantity"]
        )
        # Every row admitted before issuance is frozen after the boundary commits.
        for row in history:
            with pytest.raises(DBAPIError) as error:
                write(
                    app_connection,
                    a,
                    lines.update()
                    .where(lines.c.id == row["id"])
                    .values(quantity=99)
                    .returning(lines),
                )
            assert error.value.orig.sqlstate == "23514"
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        engine.dispose()


def test_failed_issue_transaction_leaves_draft_and_lines_unchanged(
    draft,
    space_data,
    app_connection,
    migrator_connection,
):
    po, line = draft
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, space_data[0])
            app_connection.execute(
                orders.update().where(orders.c.id == po["id"]).values(status="ISSUED")
            )
            app_connection.execute(
                lines.update().where(lines.c.id == line["id"]).values(quantity=2)
            )
    assert error.value.orig.sqlstate == "23514"
    assert snapshot(migrator_connection, orders, po["id"]) == dict(po)
    assert snapshot(migrator_connection, lines, line["id"]) == dict(line)


def test_concurrent_forward_reference_cycle_has_no_committed_rows(
    issued,
    space_data,
    database,
    migrator_connection,
):
    a, po = space_data[0], issued[0]
    ids, pids = [uuid7(), uuid7()], Queue()
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        migrator_connection.execute(
            select(orders).where(orders.c.id == po["id"]).with_for_update(key_share=True)
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    contender,
                    engine,
                    a,
                    pids,
                    lines.insert()
                    .values(line_values(a, po["id"], id=ids[i], supersedes_line_id=ids[1 - i]))
                    .returning(lines),
                )
                for i in range(2)
            ]
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10) for _ in futures])
            finally:
                migrator_connection.commit()
            assert [f.result(timeout=20)[0] for f in futures] == ["23503", "23503"]
        assert (
            migrator_connection.execute(select(lines.c.id).where(lines.c.id.in_(ids))).all() == []
        )
    finally:
        migrator_connection.rollback()
        engine.dispose()


def test_line_creation_waits_for_catalog_uom_change_and_captures_committed_default(
    draft,
    space_data,
    database,
    app_connection,
    migrator_connection,
):
    a, po = space_data[0], draft[0]
    migrator_connection.execute(items.update().where(items.c.id == a.item_id).values(uom="M"))
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            future = pool.submit(
                contender,
                engine,
                a,
                pids,
                lines.insert().values(line_values(a, po["id"], line_number=2)).returning(lines),
            )
            try:
                wait_blocked(migrator_connection, [pids.get(timeout=10)])
            finally:
                migrator_connection.commit()
            status, rows = future.result(timeout=20)
        assert status == "success" and rows[0]["uom"] == "M"
        assert snapshot(migrator_connection, lines, draft[1]["id"])["uom"] == "EA"
    finally:
        migrator_connection.rollback()
        engine.dispose()
