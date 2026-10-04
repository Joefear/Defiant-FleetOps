"""Correction stays separate from supersession and serializes with first receiving binding."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from queue import Queue
from time import monotonic, sleep

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import WHEN, receipt_data
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from fleetops.db import metadata as db
from fleetops.domain import procurement, receiving, record_corrections
from fleetops.domain.assets import AssetConflict


def test_repeat_procurement_materializes_head_and_preserves_supersession(
    space_data,
    make_item,
    make_order,
    app_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        amendment = procurement.create_line(
            app_connection,
            po["id"],
            performer_id=tenant.actor_id,
            predecessor_id=lines[0]["id"],
            values=dict(item_id=item, quantity=2, unit_price=10, expected_date=None),
        )
        original = dict(
            app_connection.execute(
                select(db.purchase_order_lines).where(
                    db.purchase_order_lines.c.id == amendment["id"]
                )
            )
            .mappings()
            .one()
        )
    for generation, change in enumerate(
        (dict(quantity=Decimal("3.125")), dict(unit_price=Decimal("19.25")))
    ):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            result = record_corrections.correct_procurement(
                app_connection,
                po["id"],
                amendment["id"],
                values=dict(
                    expected_generation=generation,
                    reason="Record transcription",
                    correction_occurred_at=WHEN,
                    **change,
                ),
            )
            assert result["quantity"] == Decimal("3.125")
            assert result["po_line_id"] == amendment["id"]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert result["unit_price"] == Decimal("19.25")
        stored = dict(
            app_connection.execute(
                select(db.purchase_order_lines).where(
                    db.purchase_order_lines.c.id == amendment["id"]
                )
            )
            .mappings()
            .one()
        )
        assert stored == original and stored["supersedes_line_id"] == lines[0]["id"]
        new_expectation = procurement.create_line(
            app_connection,
            po["id"],
            performer_id=tenant.actor_id,
            predecessor_id=amendment["id"],
            values=dict(item_id=item, quantity=7, unit_price=30, expected_date=None),
        )
        assert new_expectation["supersedes_line_id"] == amendment["id"]
        assert not procurement.get_line(app_connection, po["id"], amendment["id"])["active"]


@pytest.mark.parametrize("first", ["binding", "correction"])
def test_first_binding_and_procurement_correction_share_po_lock(
    first,
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
    root = lines[0]["id"]
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    pids = Queue()

    def correct(connection):
        return record_corrections.correct_procurement(
            connection,
            po["id"],
            root,
            values=dict(
                expected_generation=0,
                quantity=2,
                reason="Transcription",
                correction_occurred_at=WHEN,
            ),
        )

    def bind(connection, generation):
        return receiving.create_receipt(
            connection,
            org_id=tenant.org_id,
            values=receipt_data(
                tenant,
                po_id=po["id"],
                comparator_bindings=[dict(po_line_id=root, expected_generation=generation)],
            ),
        )

    def contender():
        try:
            with engine.begin() as connection:
                set_authenticated(connection, tenant)
                pids.put(connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                return correct(connection) if first == "binding" else bind(connection, 1)
        except AssetConflict:
            return "referenced"

    try:
        with ThreadPoolExecutor(max_workers=1) as workers:
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                procurement.get_order(app_connection, po["id"], lock=True)
                future = workers.submit(contender)
                pid = pids.get(timeout=10)
                deadline = monotonic() + 10
                while not migrator_connection.execute(
                    text("SELECT cardinality(pg_blocking_pids(:pid))>0"), dict(pid=pid)
                ).scalar_one():
                    migrator_connection.rollback()
                    assert monotonic() < deadline, "Contender never reached governing PO lock"
                    sleep(0.01)
                migrator_connection.rollback()
                winner = bind(app_connection, 0) if first == "binding" else correct(app_connection)
            loser = future.result(timeout=15)
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            binding = (
                app_connection.execute(
                    select(db.receipt_comparators).where(
                        db.receipt_comparators.c.po_line_id == root
                    )
                )
                .mappings()
                .one()
            )
            if first == "binding":
                assert loser == "referenced"
                assert binding["source_generation"] == 0 and binding["source_id"] is None
                assert (
                    app_connection.execute(select(db.purchase_order_line_corrections.c.id)).all()
                    == []
                )
            else:
                assert loser["id"] == binding["receipt_id"]
                assert binding["source_generation"] == 1 and binding["source_id"] == winner["id"]
    finally:
        app_connection.rollback()
        engine.dispose()
