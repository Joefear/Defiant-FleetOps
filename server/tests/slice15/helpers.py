"""Public API-only scenario operations, with expected HTTP outcomes checked at each step."""

from uuid import uuid4

WHEN = "2026-10-01T12:00:00Z"
EARLY = "1970-01-01T00:00:00Z"
PRICE = "123.12345678901234567890123456789"


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def call(client, tenant, method, path, body=None, *, status=200):
    response = client.request(method, path, headers=headers(tenant), json=body)
    assert response.status_code == status, response.text
    return response.json()


def create_item(client, tenant, name, serialized=True):
    return call(
        client,
        tenant,
        "POST",
        "/items",
        {
            "manufacturer_party_id": str(tenant.party_id),
            "manufacturer_part_number": name,
            "description": name,
            "uom": "EA",
            "serialized": serialized,
        },
        status=201,
    )["id"]


def capture_line(tenant, item, comparator=None, *, unreadable=False, serialized=True):
    identifier = {
        "type": "MANUFACTURER_SERIAL",
        "value": None if unreadable else str(uuid4()),
        "unreadable_reason": "Torn manufacturer label" if unreadable else None,
    }
    return {
        "item_id": str(item),
        "po_line_id": comparator,
        "quantity": "1",
        "uom": "EA",
        "condition": "GOOD",
        "unit": {
            "owner_party_id": str(tenant.party_id),
            "asset_tag": str(uuid4()),
            "description": "Observed unit",
            "identifier": identifier,
        }
        if serialized
        else None,
    }


def receive(client, tenant, lines, *, order=None, comparators=None):
    return call(
        client,
        tenant,
        "POST",
        "/receipts",
        {
            "vendor_party_id": str(tenant.vendor_id),
            "dock_location_id": str(tenant.location_id),
            "received_at": WHEN,
            "po_id": order,
            "comparator_ids": comparators or [],
            "lines": lines,
        },
        status=201,
    )


def current(client, tenant, asset):
    return call(client, tenant, "GET", f"/assets/{asset}")


def transition(client, tenant, asset, target, evidence=None):
    before = current(client, tenant, asset)
    return call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/transitions",
        {
            "expected_version": before["version"],
            "from_state": before["current_state"],
            "to_state": target,
            "reason": "Observed lifecycle change",
            "occurred_at": WHEN,
            "evidence_ref": evidence,
        },
        status=201,
    )


def move(client, tenant, asset, destination, *, when=WHEN):
    return call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/movements",
        {
            "expected_version": current(client, tenant, asset)["version"],
            "to_location_id": str(destination) if destination else None,
            "reason": "Scanned physical location",
            "occurred_at": when,
        },
        status=201,
    )


def attach(client, tenant, entity_type, entity_id, role, content=b"Observed evidence"):
    response = client.post(
        "/attachments",
        headers=headers(tenant),
        content=content,
        params={
            "source_type": "DOCUMENT",
            "captured_at": WHEN,
            "original_filename": "observation.txt",
            "media_type": "text/plain",
        },
    )
    assert response.status_code == 201, response.text
    attachment = response.json()
    linked = call(
        client,
        tenant,
        "POST",
        f"/attachments/{attachment['id']}/links",
        {
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "link_role": role,
        },
        status=201,
    )
    return attachment, linked


def entries(history, kind):
    return [entry for entry in history["entries"] if entry["kind"] == kind]
