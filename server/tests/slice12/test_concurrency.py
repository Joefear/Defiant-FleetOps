"""Deterministic row contention and filesystem-survives-rollback delivery proofs."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

from server.tests.auth_context import set_authenticated
from server.tests.slice12.conftest import headers, queue, template
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from fleetops.labels import service
from fleetops.labels.adapters.registry import registry


def test_two_dispatchers_contend_and_publish_one_job(
    label_client, label_root, asset_data, database, app_connection
):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    job = queue(label_client, tenant, record["id"]).json()
    adapters = registry(label_root)
    started = Event()
    worker_pid = []
    engine = create_engine(database.url("fleetops_app"), poolclass=NullPool)

    def dispatch_worker():
        with engine.begin() as connection:
            set_authenticated(connection, tenant)
            worker_pid.append(connection.execute(text("SELECT pg_backend_pid()")).scalar_one())
            started.set()
            return dict(service.dispatch(connection, UUID(job["id"]), adapters))

    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        service.get_job(app_connection, UUID(job["id"]), lock=True)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(dispatch_worker)
            try:
                assert started.wait(5)
                deadline = time.monotonic() + 5
                while not app_connection.execute(
                    text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=:pid AND NOT granted)"),
                    {"pid": worker_pid[0]},
                ).scalar_one():
                    assert time.monotonic() < deadline, (
                        "Second dispatcher never reached row contention"
                    )
                    time.sleep(0.01)
                first = dict(service.dispatch(app_connection, UUID(job["id"]), adapters))
                app_connection.commit()
                second = future.result(timeout=10)
            finally:
                # Release the lock before executor shutdown, including assertion failure.
                app_connection.rollback()
        assert first == second
        assert first["status"] == "SUCCEEDED" and first["attempts"] == 1
        assert len(list(label_root.rglob("*.png"))) == 1
    finally:
        app_connection.rollback()
        engine.dispose()


def test_retry_reuses_output_after_delivery_transaction_rolls_back(
    label_client, label_root, asset_data, app_connection
):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    job = queue(label_client, tenant, record["id"], output_format="PDF").json()
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        discarded = dict(service.dispatch(app_connection, UUID(job["id"]), registry(label_root)))
        assert discarded["status"] == "SUCCEEDED"
    finally:
        app_connection.rollback()
    artifact = label_root / str(tenant.org_id) / (job["id"] + ".pdf")
    original = artifact.read_bytes()
    assert (
        label_client.get(f"/print-jobs/{job['id']}", headers=headers(tenant)).json()["status"]
        == "PENDING"
    )
    repeated = label_client.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["attempts"] == 1
    assert repeated.json()["artifact_sha256"] == discarded["artifact_sha256"]
    assert artifact.read_bytes() == original
