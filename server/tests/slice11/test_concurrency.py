"""Observe actual PostgreSQL contention before releasing each declared winner or abort."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from queue import Queue
from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import runtime_transition
from server.tests.slice10.test_concurrency import wait_for_holder
from server.tests.slice11.conftest import link, upload
from sqlalchemy import create_engine, select, text
from sqlalchemy.pool import NullPool

from fleetops.db.metadata import asset_movements, asset_transitions, assets, attachments
from fleetops.domain import asset_facts, corrections
from fleetops.domain import assets as asset_service
from fleetops.domain.assets import AssetConflict
from fleetops.evidence.service import capture
from fleetops.evidence.storage import FilesystemEvidenceStorage

WHEN = datetime(2026, 1, 2, tzinfo=UTC)


@pytest.mark.parametrize("commit_holder", [True, False])
def test_duplicate_upload_waits_for_metadata_commit_or_abort(
    commit_holder, database, asset_data, evidence_root, migrator_connection
):
    """The unique key, not a pre-insert SELECT, arbitrates first-capture provenance."""
    tenant = asset_data[0]
    storage = FilesystemEvidenceStorage(evidence_root)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    worker_pid = Queue()
    values = dict(
        source_type="DOCUMENT",
        captured_at=WHEN,
        media_type="text/plain",
        original_filename="holder.txt",
    )

    def contender():
        with engine.begin() as connection:
            set_authenticated(connection, tenant)
            worker_pid.put(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            return dict(
                capture(
                    connection,
                    storage,
                    tenant.org_id,
                    b"simultaneous capture",
                    dict(values, original_filename="worker.txt"),
                )
            )

    try:
        with engine.connect() as holder, ThreadPoolExecutor(max_workers=1) as workers:
            transaction = holder.begin()
            try:
                set_authenticated(holder, tenant)
                holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
                first = dict(
                    capture(holder, storage, tenant.org_id, b"simultaneous capture", values)
                )
                future = workers.submit(contender)
                wait_for_holder(migrator_connection, worker_pid.get(timeout=10), holder_pid)
                assert not future.done()
                if commit_holder:
                    transaction.commit()
                else:
                    transaction.rollback()
                result = future.result(timeout=15)
            finally:
                if transaction.is_active:
                    transaction.rollback()
        persisted = (
            migrator_connection.execute(
                select(attachments).where(attachments.c.org_id == tenant.org_id)
            )
            .mappings()
            .all()
        )
        assert len(persisted) == 1 and dict(persisted[0]) == result
        assert (result["id"] == first["id"]) is commit_holder
        assert result["original_filename"] == ("holder.txt" if commit_holder else "worker.txt")
        assert (
            storage.read(
                tenant.org_id,
                storage.put(tenant.org_id, b"simultaneous capture"),
            )
            == b"simultaneous capture"
        )
        assert len(list((evidence_root / str(tenant.org_id)).iterdir())) == 1
    finally:
        migrator_connection.rollback()
        engine.dispose()


@pytest.mark.parametrize("winner", ["retirement", "movement"])
@pytest.mark.parametrize("corrected", [False, True])
def test_retirement_and_movement_share_the_global_version_lock(
    winner,
    corrected,
    database,
    asset_data,
    evidence_client,
    evidence_root,
    app_connection,
    migrator_connection,
):
    """Both commit orders have one winner; the blocked loser cannot append stale history."""
    tenant = asset_data[0]
    row = upload(evidence_client, tenant).json()
    assert link(evidence_client, tenant, row["id"]).status_code == 201
    runtime_transition(app_connection, tenant)
    root = None
    expected_version = 2
    if corrected:
        root = runtime_transition(
            app_connection,
            tenant,
            expected_version=2,
            from_state="IN_STOCK",
            to_state="CONFIGURING",
        )
        expected_version = 3
    storage = FilesystemEvidenceStorage(evidence_root)
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)
    worker_pid = Queue()

    def operation(connection, kind):
        values = dict(
            expected_version=expected_version,
            reason="Competing physical operation",
            occurred_at=WHEN,
        )
        if kind == "retirement":
            if corrected:
                return corrections.correct_transition(
                    connection,
                    tenant.asset_id,
                    root["id"],
                    storage=storage,
                    values=dict(
                        values,
                        to_state="RETIRED",
                        evidence_ref=UUID(row["id"]),
                        correction_occurred_at=WHEN,
                    ),
                )
            return asset_service.transition_asset(
                connection,
                tenant.asset_id,
                storage=storage,
                values=dict(
                    values, from_state="IN_STOCK", to_state="RETIRED", evidence_ref=UUID(row["id"])
                ),
            )
        return asset_facts.move_asset(
            connection,
            tenant.asset_id,
            values=dict(values, to_location_id=tenant.other_location_id),
        )

    def contender():
        try:
            with engine.begin() as connection:
                set_authenticated(connection, tenant)
                worker_pid.put(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
                operation(connection, "movement" if winner == "retirement" else "retirement")
            return "accepted"
        except AssetConflict:
            return "stale"

    try:
        with engine.connect() as holder, ThreadPoolExecutor(max_workers=1) as workers:
            transaction = holder.begin()
            try:
                set_authenticated(holder, tenant)
                holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
                operation(holder, winner)
                expected = {
                    table.name: holder.execute(
                        select(table)
                        .where(table.c.org_id == tenant.org_id)
                        .order_by(*table.primary_key.columns)
                    ).all()
                    for table in (assets, asset_transitions, asset_movements)
                }
                future = workers.submit(contender)
                wait_for_holder(migrator_connection, worker_pid.get(timeout=10), holder_pid)
                transaction.commit()
                assert future.result(timeout=15) == "stale"
            finally:
                if transaction.is_active:
                    transaction.rollback()
        with engine.begin() as connection:
            set_authenticated(connection, tenant)
            for table in (assets, asset_transitions, asset_movements):
                assert (
                    connection.execute(
                        select(table)
                        .where(table.c.org_id == tenant.org_id)
                        .order_by(*table.primary_key.columns)
                    ).all()
                    == expected[table.name]
                )
            assert connection.execute(
                select(assets.c.version).where(assets.c.id == tenant.asset_id)
            ).scalar_one() == expected_version + (2 if corrected and winner == "retirement" else 1)
            assert not asset_facts.reconcile_assets(connection)
    finally:
        migrator_connection.rollback()
        engine.dispose()
