"""Capture reuses the accepted receiving, evidence and Exception workflows."""

from uuid import UUID, uuid4

from server.tests.slice13.conftest import headers, operation, submit
from sqlalchemy import select

from fleetops.db import metadata as db


def test_received_unit_is_created_once_and_clock_claim_is_preserved(
    capture_client, asset_data, receiving_cleanup, migrator_connection
):
    tenant = asset_data[0]
    receipt = capture_client.post(
        "/receipts",
        headers=headers(tenant),
        json={
            "vendor_party_id": str(tenant.vendor_id),
            "dock_location_id": str(tenant.location_id),
            "received_at": "2026-10-04T12:00:00Z",
            "reconcile": False,
        },
    )
    assert receipt.status_code == 201, receipt.text
    op = operation(
        tenant,
        operation="RECEIVE_SCAN",
        entity_type="RECEIPT",
        entity_id=receipt.json()["id"],
        expected_version=None,
        payload={
            "line": {
                "item_id": str(tenant.item_id),
                "quantity": "1",
                "uom": "EA",
                "condition": "GOOD",
                "unit": {
                    "owner_party_id": str(tenant.vendor_id),
                    "asset_tag": "SCANNED-001",
                    "description": "Offline scanned workstation",
                    "identifier": {"type": "MANUFACTURER_SERIAL", "value": "OFFLINE-SERIAL"},
                },
            },
            "reconcile": True,
        },
    )
    first = submit(capture_client, tenant, [op])[0]
    assert first["sync_state"] == "APPLIED", first
    second = submit(capture_client, tenant, [op])[0]
    assert second["sync_state"] == "DUPLICATE" and second["result"] == first["result"]
    actual = capture_client.get("/receipts/" + receipt.json()["id"], headers=headers(tenant)).json()
    assert len(actual["lines"]) == 1 and actual["reconciled"]
    assert actual["lines"][0]["asset_id"] is not None
    assert actual["lines"][0]["id"] == first["result"]["line_id"]
    captured = (
        migrator_connection.execute(
            select(db.capture_operations).where(
                db.capture_operations.c.operation_id == UUID(op["operation_id"])
            )
        )
        .mappings()
        .one()
    )
    assert captured["occurred_at"].year == 1970
    # The raw receiving record retains its physical receipt-time claim; operation
    # capture time is an additional immutable fact, not a receipt-header overwrite.
    assert actual["received_at"] == "2026-10-04T12:00:00Z"
    # Extending the Exception surface with Asset sync conflicts must retain the
    # receiving primary-context boundary, without exposing another tenant's IDs.
    for receiving_id in (actual["id"], actual["lines"][0]["id"]):
        params = {"entity_type": "ASSET", "entity_id": receiving_id}
        assert (
            capture_client.get(
                "/exceptions/open", headers=headers(tenant), params=params
            ).status_code
            == 422
        )
        hidden = capture_client.get(
            "/exceptions/open", headers=headers(asset_data[1]), params=params
        )
        assert hidden.status_code == 200 and hidden.json() == []
    for asset_id in (tenant.asset_id, uuid4()):
        empty = capture_client.get(
            "/exceptions/open",
            headers=headers(tenant),
            params={"entity_type": "ASSET", "entity_id": str(asset_id)},
        )
        assert empty.status_code == 200 and empty.json() == []
    exception = actual["exceptions"][0]["id"]
    resolve = operation(
        tenant,
        operation="RESOLVE",
        entity_type="EXCEPTION",
        entity_id=exception,
        expected_version=None,
        payload={"expected_status": "OPEN", "note": "Checked delivery"},
    )
    resolved = submit(capture_client, tenant, [resolve])[0]
    assert resolved["sync_state"] == "APPLIED"
    assert (
        capture_client.get("/exceptions/" + exception, headers=headers(tenant)).json()["status"]
        == "RESOLVED"
    )


def test_evidence_link_ignores_asset_version_but_verifies_stored_bytes(
    capture_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    response = capture_client.post(
        "/attachments",
        headers=headers(tenant),
        params={
            "source_type": "PHOTO",
            "captured_at": "1970-01-01T00:00:00Z",
            "original_filename": "serial-photo.png",
            "media_type": "image/png",
        },
        content=b"Photograph evidence bytes",
    )
    assert response.status_code == 201, response.text
    op = operation(
        tenant,
        operation="ATTACH_EVIDENCE",
        expected_version=2147483647,
        payload={"attachment_id": response.json()["id"], "link_role": "OTHER"},
    )
    first = submit(capture_client, tenant, [op])[0]
    assert first["sync_state"] == "APPLIED" and first["result"]["entity_id"] == str(tenant.asset_id)
    assert submit(capture_client, tenant, [op])[0]["sync_state"] == "DUPLICATE"
    assert (
        migrator_connection.execute(
            select(db.assets.c.version).where(db.assets.c.id == tenant.asset_id)
        ).scalar_one()
        == 1
    )
    assert (
        migrator_connection.execute(select(db.attachment_links)).mappings().one()["actor_id"]
        == tenant.actor_id
    )


def test_cross_tenant_attachment_or_invalid_role_is_rejected(
    capture_client, asset_data, migrator_connection
):
    tenant, other = asset_data
    attachment = capture_client.post(
        "/attachments",
        headers=headers(other),
        params={
            "source_type": "PHOTO",
            "captured_at": "1970-01-01T00:00:00Z",
            "original_filename": "serial.png",
            "media_type": "image/png",
        },
        content=b"Other tenant capture",
    ).json()["id"]
    row = submit(
        capture_client,
        tenant,
        [
            operation(
                tenant,
                operation="ATTACH_EVIDENCE",
                payload={"attachment_id": attachment, "link_role": "OTHER"},
            )
        ],
    )[0]
    assert row["sync_state"] == "REJECTED" and row["result"]["code"] == "NOT_FOUND"
    assert migrator_connection.execute(select(db.attachment_links)).all() == []
