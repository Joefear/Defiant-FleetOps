"""The frozen Section 9 scenario is executable through the authenticated public API."""

from collections import Counter
from datetime import datetime
from io import BytesIO
from uuid import uuid4

import zxingcpp
from PIL import Image
from server.tests.slice15.conftest import (
    receiving_cleanup,
    scenario_client,
    space_data,
    space_password_hash,
)
from server.tests.slice15.helpers import (
    EARLY,
    PRICE,
    WHEN,
    attach,
    call,
    capture_line,
    create_item,
    current,
    entries,
    headers,
    move,
    receive,
    transition,
)
from sqlalchemy import select

from fleetops.db.metadata import assets, purchase_order_lines

__all__ = ["receiving_cleanup", "scenario_client", "space_data", "space_password_hash"]


def test_v0_1_scenario(scenario_client, space_data, migrator_connection):
    """Receive, label, capture offline, correct, replace, retire and reconstruct via the API."""
    client, tenant = scenario_client, space_data[0]
    workstation, substitute, scanner, printer, network = [
        create_item(client, tenant, name)
        for name in ("workstation", "substitution", "scanner", "printer", "network")
    ]
    accessory = create_item(client, tenant, "accessory", serialized=False)
    order = call(
        client,
        tenant,
        "POST",
        "/purchase-orders",
        {
            "vendor_party_id": str(tenant.vendor_id),
            "po_number": "SECTION-9-" + str(uuid4()),
        },
        status=201,
    )
    lines = [
        call(
            client,
            tenant,
            "POST",
            f"/purchase-orders/{order['id']}/lines",
            {
                "line_number": number,
                "item_id": item,
                "quantity": str(quantity),
                "unit_price": PRICE,
            },
            status=201,
        )
        for number, (item, quantity) in enumerate(
            ((workstation, 6), (scanner, 2), (printer, 1), (network, 1)), 1
        )
    ]
    call(client, tenant, "POST", f"/purchase-orders/{order['id']}/issue")
    issued = migrator_connection.execute(
        select(purchase_order_lines).where(purchase_order_lines.c.po_id == order["id"])
    ).all()
    migrator_connection.rollback()
    receipt = receive(
        client,
        tenant,
        [
            capture_line(tenant, workstation, lines[0]["id"], unreadable=index == 0)
            for index in range(5)
        ]
        + [capture_line(tenant, substitute, lines[0]["id"])]
        + [
            capture_line(tenant, item, line["id"])
            for item, line in zip((scanner, printer, network), lines[1:], strict=True)
        ]
        + [capture_line(tenant, accessory, serialized=False)],
        order=order["id"],
        comparators=[line["id"] for line in lines],
    )
    assert Counter(e["exception_type"] for e in receipt["exceptions"]) == Counter(
        {
            "SUBSTITUTION": 1,
            "SERIAL_UNREADABLE": 1,
            "SHORT": 1,
            "UNEXPECTED_ITEM": 1,
        }
    )
    assert len(receipt["lines"]) == 10
    unit_ids = [line["asset_id"] for line in receipt["lines"] if line["asset_id"]]
    assert len(set(unit_ids)) == 9
    assert (
        migrator_connection.execute(
            select(purchase_order_lines).where(purchase_order_lines.c.po_id == order["id"])
        ).all()
        == issued
    )
    migrator_connection.rollback()
    receiving_attachment, _ = attach(
        client, tenant, "RECEIPT", receipt["id"], "RECEIVING_EVIDENCE", b"Arrival packing slip"
    )
    template = call(
        client,
        tenant,
        "POST",
        "/label-templates",
        {
            "name": "Section 9 identity",
            "human_fields": ["asset_tag"],
            "symbology": "CODE128",
        },
        status=201,
    )
    for asset in unit_ids:
        job = call(
            client,
            tenant,
            "POST",
            f"/assets/{asset}/labels",
            {"template_id": template["id"], "output_format": "PNG"},
            status=202,
        )
        assert (
            call(client, tenant, "POST", f"/print-jobs/{job['id']}/dispatch")["status"]
            == "SUCCEEDED"
        )
        output = client.get(f"/print-jobs/{job['id']}/content", headers=headers(tenant))
        assert output.status_code == 200
        with Image.open(BytesIO(output.content)) as image:
            scanned = zxingcpp.read_barcode(image).text
        assert scanned == asset
        assert call(client, tenant, "GET", f"/resolve/{scanned}")["entity_id"] == asset
        move(client, tenant, scanned, tenant.other_location_id)
        call(
            client,
            tenant,
            "POST",
            f"/assets/{asset}/configurations",
            {
                "image_name": "factory",
                "image_version": "1",
                "config_profile": "observed",
                "applied_at": WHEN,
            },
            status=201,
        )
        call(
            client,
            tenant,
            "POST",
            f"/assets/{asset}/assignments",
            {
                "expected_version": current(client, tenant, asset)["version"],
                "assignee_type": "ACTOR",
                "assignee_id": str(tenant.actor_id),
                "reason": "Scanned assignment",
                "occurred_at": WHEN,
            },
            status=201,
        )
    failed, replacement = unit_ids[:2]
    for state in ("IN_STOCK", "CONFIGURING", "READY", "DEPLOYED"):
        transition(client, tenant, failed, state)
    # Disconnected capture is a local claim, not a write. An independent online
    # state change wins before reconnect; the original claim must be rejected.
    before = current(client, tenant, failed)
    queued = {
        "operation_id": str(uuid4()),
        "actor_id": str(tenant.actor_id),
        "client_id": str(uuid4()),
        "client_epoch": str(uuid4()),
        "client_seq": 1,
        "entity_type": "ASSET",
        "entity_id": failed,
        "expected_version": before["version"],
        "operation": "TRANSITION",
        "occurred_at": EARLY,
        "payload": {"from_state": "DEPLOYED", "to_state": "ON_HOLD", "reason": "Offline claim"},
    }
    transition(client, tenant, failed, "READY")
    batch = call(client, tenant, "POST", "/capture/operations", {"operations": [queued]})
    rejected = batch[0]
    assert rejected["sync_state"] == "REJECTED" and rejected["result"]["code"] == "SYNC_CONFLICT"
    assert current(client, tenant, failed)["current_state"] == "READY"
    conflict = call(client, tenant, "GET", "/exceptions/open?asset_id=" + failed)[0]
    call(
        client,
        tenant,
        "POST",
        f"/exceptions/{conflict['id']}/events",
        {
            "expected_status": "OPEN",
            "to_status": "RESOLVED",
            "note": "Keep the witnessed online change",
            "occurred_at": WHEN,
        },
        status=201,
    )
    # A mistaken location record is cancelled administratively, never represented
    # as an invented physical return. Clock claims deliberately disagree with versions.
    mistake = move(client, tenant, failed, tenant.location_id, when=EARLY)
    corrected = call(
        client,
        tenant,
        "POST",
        f"/assets/{failed}/movements/{mistake['id']}/corrections",
        {
            "expected_version": current(client, tenant, failed)["version"],
            "to_location_id": None,
            "reason": "Mis-keyed destination; actual location unknown",
            "correction_occurred_at": WHEN,
            "occurred_at": EARLY,
        },
        status=201,
    )
    call(
        client,
        tenant,
        "POST",
        f"/assets/{failed}/custody-changes",
        {
            "expected_version": current(client, tenant, failed)["version"],
            "to_custodian_party_id": str(tenant.vendor_id),
            "reason": "Observed custody",
            "occurred_at": WHEN,
        },
        status=201,
    )
    call(
        client,
        tenant,
        "POST",
        f"/assets/{failed}/ownership-changes",
        {
            "expected_version": current(client, tenant, failed)["version"],
            "to_owner_party_id": str(tenant.vendor_id),
            "reason": "Recorded title change",
            "occurred_at": WHEN,
        },
        status=201,
    )
    transition(client, tenant, failed, "DEPLOYED")
    transition(client, tenant, failed, "OUT_OF_SERVICE")
    call(
        client,
        tenant,
        "POST",
        f"/assets/{failed}/unassignment",
        {
            "expected_version": current(client, tenant, failed)["version"],
            "reason": "Failed workstation removed",
            "occurred_at": WHEN,
        },
        status=201,
    )
    call(
        client,
        tenant,
        "POST",
        f"/assets/{replacement}/assignments",
        {
            "expected_version": current(client, tenant, replacement)["version"],
            "assignee_type": "LOCATION",
            "assignee_id": str(tenant.other_location_id),
            "reason": "Replacement installed at station",
            "occurred_at": WHEN,
        },
        status=201,
    )
    disposal, _ = attach(
        client, tenant, "ASSET", failed, "DISPOSAL_EVIDENCE", b"Physical disposal certificate"
    )
    transition(client, tenant, failed, "RETIRED", disposal["id"])
    history = call(client, tenant, "GET", f"/assets/{failed}/history")
    assert history["asset"]["current_state"] == "RETIRED"
    assert history["asset"]["current_assignment_id"] is None
    assert history["asset"]["current_location_id"] is None
    assert all(
        e["actor_id"] == str(tenant.actor_id) and e["recorded_at"] and e["occurred_at"]
        for e in history["entries"]
    )
    assert [datetime.fromisoformat(e["occurred_at"]) for e in history["entries"]] == sorted(
        datetime.fromisoformat(e["occurred_at"]) for e in history["entries"]
    )
    counts = Counter(e["kind"] for e in history["entries"])
    assert counts["TRANSITION"] == 9 and counts["MOVEMENT"] == 4
    assert counts["ASSIGNMENT"] == 2 and counts["CONFIGURATION"] == 1
    assert counts["CUSTODY"] == counts["OWNERSHIP"] == 1
    assert counts["SYNC_CONFLICT"] == counts["SYNC_CONFLICT_EVENT"] == 1
    assert counts["INITIAL_FACTS"] == counts["INITIAL_ASSIGNMENT"] == 1
    assert counts["RECEIPT"] == counts["RECEIPT_LINE"] == counts["RECEIPT_COMPARATOR"] == 1
    assert counts["IDENTIFIER"] == 1 and counts["EXCEPTION"] == 1
    assert entries(history, "EXCEPTION")[0]["facts"]["exception_type"] == "SERIAL_UNREADABLE"
    assert entries(history, "RECEIPT")[0]["facts"]["vendor_party_id"] == str(tenant.vendor_id)
    assert (
        entries(history, "RECEIPT_COMPARATOR")[0]["facts"]["expected_line"]["unit_price"] == PRICE
    )
    movement = entries(history, "MOVEMENT")
    reversal = next(e for e in movement if e["facts"]["correction_role"] == "REVERSAL")
    assert reversal["cancels_history_id"] == mistake["id"]
    assert reversal["facts"]["correction_pair_id"] == corrected["correction_pair_id"]
    assert {e["id"] for e in entries(history, "ATTACHMENT")} == {
        receiving_attachment["id"],
        disposal["id"],
    }
    versions = sorted(
        e["result_version"] for e in history["entries"] if e["result_version"] is not None
    )
    assert versions == list(range(1, history["asset"]["version"] + 1))
    health = call(client, tenant, "GET", "/health/reconciliation")
    assert health == {
        "discrepancy_count": 0,
        "affected_asset_count": 0,
        "affected_record_count": 0,
        "assets": [],
        "corrections": [],
    }
    # The test owner deliberately corrupts only this disposable database. The
    # runtime reports one affected unit, preserves the corruption, and repairs nothing.
    migrator_connection.execute(
        assets.update().where(assets.c.id == failed).values(current_location_id=tenant.location_id)
    )
    migrator_connection.commit()
    health = call(client, tenant, "GET", "/health/reconciliation")
    assert health["discrepancy_count"] == health["affected_asset_count"] == 1
    assert health["assets"][0]["asset_id"] == failed
    assert health["assets"][0]["discrepancies"] == ["movement_projection_mismatch"]
    assert current(client, tenant, failed)["current_location_id"] == str(tenant.location_id)
