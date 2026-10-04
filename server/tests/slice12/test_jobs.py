"""Authenticated queue, immutable request snapshots, visible failures and real downloads."""

from io import BytesIO

import pytest
import zxingcpp
from fastapi.testclient import TestClient
from PIL import Image
from server.tests.slice9.conftest import line_data, post_receipt, receipt_data
from server.tests.slice12.conftest import headers, queue, template
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.db.metadata import assets, print_jobs


def test_request_commits_before_output_then_dispatches_without_asset_version_change(
    label_client, label_root, asset_data, migrator_connection
):
    tenant = asset_data[0]
    created = template(label_client, tenant)
    assert created.status_code == 201, created.text
    assert created.json()["created_by_actor_id"] == str(tenant.actor_id)
    job = queue(label_client, tenant, created.json()["id"])
    assert job.status_code == 202, job.text
    assert job.json()["requested_by"] == str(tenant.actor_id)
    assert job.json()["status"] == "PENDING" and job.json()["attempts"] == 0
    assert not label_root.exists()
    persisted = migrator_connection.execute(select(print_jobs)).mappings().one()
    assert persisted["status"] == "PENDING" and persisted["render_snapshot"]["id"] == str(
        tenant.asset_id
    )
    migrator_connection.rollback()
    response = label_client.post(
        f"/print-jobs/{job.json()['id']}/dispatch", headers=headers(tenant)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "SUCCEEDED"
    assert response.json()["updated_by_actor_id"] == str(tenant.actor_id)
    assert response.json()["attempts"] == 1
    content = label_client.get(f"/print-jobs/{job.json()['id']}/content", headers=headers(tenant))
    assert content.status_code == 200
    assert content.headers["Cache-Control"] == "no-store"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert content.headers["Content-Disposition"].startswith("attachment;")
    with Image.open(BytesIO(content.content)) as image:
        assert zxingcpp.read_barcode(image).text == str(tenant.asset_id)
    assert (
        migrator_connection.execute(
            select(assets.c.version).where(assets.c.id == tenant.asset_id)
        ).scalar_one()
        == 1
    )
    migrator_connection.rollback()
    repeated = label_client.post(
        f"/print-jobs/{job.json()['id']}/dispatch", headers=headers(tenant)
    )
    assert repeated.json() == response.json()
    assert len(list(label_root.rglob("*.png"))) == 1


def test_queued_label_retains_original_text_after_asset_metadata_changes(
    label_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    old = queue(label_client, tenant, record["id"]).json()
    response = label_client.patch(
        f"/assets/{tenant.asset_id}", headers=headers(tenant), json={"asset_tag": "NEW-TAG"}
    )
    assert response.status_code == 200, response.text
    new = queue(label_client, tenant, record["id"]).json()
    snapshots = {
        row["id"]: row["render_snapshot"]
        for row in migrator_connection.execute(select(print_jobs)).mappings()
    }
    assert (
        next(s["asset_tag"] for key, s in snapshots.items() if str(key) == old["id"]) == "FLEET-001"
    )
    assert (
        next(s["asset_tag"] for key, s in snapshots.items() if str(key) == new["id"]) == "NEW-TAG"
    )
    migrator_connection.rollback()
    results = []
    for job in (old, new):
        done = label_client.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant))
        assert done.status_code == 200, done.text
        results.append(done.json()["artifact_sha256"])
        content = label_client.get(f"/print-jobs/{job['id']}/content", headers=headers(tenant))
        with Image.open(BytesIO(content.content)) as image:
            assert zxingcpp.read_barcode(image).text == str(tenant.asset_id)
    assert results[0] != results[1]


def test_missing_output_configuration_records_failure_then_retry_succeeds(
    database, asset_data, label_root, label_client
):
    tenant = asset_data[0]
    with TestClient(create_app(database.settings(tenant.org_id))) as unconfigured:
        record = template(unconfigured, tenant).json()
        job = queue(unconfigured, tenant, record["id"]).json()
        failed = unconfigured.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant))
        assert failed.status_code == 503
        assert failed.json()["status"] == "FAILED"
        assert failed.json()["error_code"] == "OUTPUT_UNAVAILABLE"
        assert failed.json()["attempts"] == 1
        assert (
            unconfigured.get(f"/print-jobs/{job['id']}", headers=headers(tenant)).json()
            == failed.json()
        )
    assert not label_root.exists()
    done = label_client.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant))
    assert done.status_code == 200 and done.json()["attempts"] == 2


def test_artifact_tampering_fails_closed(label_client, label_root, asset_data):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    job = queue(label_client, tenant, record["id"]).json()
    done = label_client.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant)).json()
    path = label_root / str(tenant.org_id) / (job["id"] + ".png")
    path.write_bytes(b"tampered")
    response = label_client.get(f"/print-jobs/{job['id']}/content", headers=headers(tenant))
    assert response.status_code == 503 and b"tampered" not in response.content
    assert str(label_root) not in response.text
    repeated = label_client.post(f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant))
    assert repeated.status_code == 503
    assert label_client.get(f"/print-jobs/{job['id']}", headers=headers(tenant)).json() == done


@pytest.mark.parametrize(
    "suffix,method",
    [
        ("", "get"),
        ("/dispatch", "post"),
        ("/content", "get"),
    ],
)
def test_foreign_jobs_are_indistinguishable_from_missing(label_client, asset_data, suffix, method):
    a, b = asset_data
    record = template(label_client, a).json()
    job = queue(label_client, a, record["id"]).json()
    request = getattr(label_client, method)
    foreign = request(f"/print-jobs/{job['id']}{suffix}", headers=headers(b))
    missing = request(f"/print-jobs/{uuid7()}{suffix}", headers=headers(b))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.parametrize(
    "field", ["org_id", "requested_by", "requested_at", "output_path", "status"]
)
def test_print_request_rejects_caller_authority(label_client, asset_data, label_root, field):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    result = queue(label_client, tenant, record["id"], **{field: "forged"})
    assert result.status_code == 422
    assert not label_root.exists()


def test_foreign_template_and_asset_cannot_be_queued(label_client, asset_data):
    a, b = asset_data
    a_template = template(label_client, a).json()
    b_template = template(label_client, b).json()
    assert queue(label_client, a, b_template["id"]).status_code == 404
    assert queue(label_client, a, a_template["id"], asset_id=b.asset_id).status_code == 404
    assert label_client.get("/label-templates", headers=headers(a)).json() == [a_template]


@pytest.mark.usefixtures("receiving_cleanup")
def test_batch_receipt_labels_exact_actual_units(label_client, asset_data, make_item, make_order):
    tenant = asset_data[0]
    item = make_item(serialized=False)
    delivery = post_receipt(
        label_client,
        tenant,
        receipt_data(
            tenant,
            lines=[
                line_data(tenant),
                line_data(tenant),
                line_data(tenant, item_id=item, quantity="20", unit=None),
            ],
        ),
    )
    record = template(label_client, tenant, symbology="DATAMATRIX").json()
    result = label_client.post(
        f"/receipts/{delivery['id']}/labels",
        headers=headers(tenant),
        json={"template_id": record["id"], "output_format": "PNG"},
    )
    assert result.status_code == 202, result.text
    assert len(result.json()) == 2
    actual = {line["asset_id"] for line in delivery["lines"] if line["asset_id"]}
    assert {job["entity_id"] for job in result.json()} == actual
    for job in result.json():
        assert (
            label_client.post(
                f"/print-jobs/{job['id']}/dispatch", headers=headers(tenant)
            ).status_code
            == 200
        )
        content = label_client.get(f"/print-jobs/{job['id']}/content", headers=headers(tenant))
        with Image.open(BytesIO(content.content)) as image:
            assert zxingcpp.read_barcode(image).text == job["entity_id"]


def test_unauthenticated_request_and_pending_download_write_nothing(
    label_client, asset_data, label_root
):
    tenant = asset_data[0]
    assert label_client.post("/label-templates", json={}).status_code == 401
    assert label_client.post(f"/assets/{tenant.asset_id}/labels", json={}).status_code == 401
    record = template(label_client, tenant).json()
    job = queue(label_client, tenant, record["id"]).json()
    assert (
        label_client.get(f"/print-jobs/{job['id']}/content", headers=headers(tenant)).status_code
        == 409
    )
    assert not label_root.exists()
