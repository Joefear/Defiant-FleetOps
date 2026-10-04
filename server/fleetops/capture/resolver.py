"""Resolve opaque internal UUIDs with explicit safe summaries inside tenant RLS."""

from sqlalchemy import select

from fleetops.db import metadata as db
from fleetops.domain.assets import AssetConflict, AssetNotFound

# Never expose users, sessions, credentials, storage keys or external lookup values.
SUMMARIES = {
    "ASSET": (
        "assets",
        "id",
        ("asset_tag", "description", "item_id", "current_state", "current_location_id", "version"),
    ),
    "ASSET_IDENTIFIER": ("asset_identifiers", "id", ("asset_id", "type", "value")),
    "LOCATION": ("locations", "id", ("name", "kind", "facility_id", "parent_location_id")),
    "FACILITY": ("facilities", "id", ("name", "timezone")),
    "ITEM": ("items", "id", ("manufacturer_part_number", "description", "serialized")),
    "PARTY": ("parties", "id", ("display_name",)),
    "ACTOR": ("actors", "id", ("display_name", "type", "active")),
    "PURCHASE_ORDER": ("purchase_orders", "id", ("po_number", "status", "vendor_party_id")),
    "PURCHASE_ORDER_LINE": ("purchase_order_lines", "id", ("po_id", "item_id", "quantity", "uom")),
    "RECEIPT": ("receipts", "id", ("vendor_party_id", "po_id", "dock_location_id", "received_at")),
    "RECEIPT_LINE": (
        "receipt_lines",
        "id",
        ("receipt_id", "item_id", "asset_id", "quantity", "uom"),
    ),
    "ASSET_CONFIGURATION": (
        "asset_configurations",
        "id",
        ("asset_id", "image_name", "image_version"),
    ),
    "ATTACHMENT": ("attachments", "id", ("sha256", "byte_size", "original_filename")),
    "EXCEPTION": ("receiving_exceptions", "id", ("exception_type", "receipt_id", "asset_id")),
    "SYNC_CONFLICT": ("sync_conflicts", "id", ("asset_id", "expected_version", "current_version")),
    "ORGANIZATION": ("organizations", "id", ("name",)),
    "EXTERNAL_REFERENCE": ("external_references", "id", ("entity_type", "entity_id", "system")),
    "PARTY_ROLE": ("party_roles", "id", ("party_id", "role")),
    "ATTACHMENT_LINK": (
        "attachment_links",
        "id",
        ("attachment_id", "entity_type", "entity_id", "link_role"),
    ),
    "CAPTURE_OPERATION": (
        "capture_operations",
        "operation_id",
        ("entity_type", "entity_id", "operation", "sync_state"),
    ),
    "ASSET_TRANSITION": (
        "asset_transitions",
        "id",
        ("asset_id", "result_version", "from_state", "to_state"),
    ),
    "ASSET_MOVEMENT": (
        "asset_movements",
        "id",
        ("asset_id", "result_version", "from_location_id", "to_location_id"),
    ),
    "ASSET_CUSTODY_CHANGE": ("asset_custody_changes", "id", ("asset_id", "result_version")),
    "ASSET_OWNERSHIP_CHANGE": ("asset_ownership_changes", "id", ("asset_id", "result_version")),
    "ASSET_ASSIGNMENT_EVENT": (
        "asset_assignment_events",
        "id",
        ("asset_id", "result_version", "to_assignee_type", "to_assignee_id"),
    ),
    "PURCHASE_ORDER_LINE_CORRECTION": (
        "purchase_order_line_corrections",
        "id",
        ("po_line_id", "correction_role", "correction_generation"),
    ),
    "RECEIPT_LINE_CORRECTION": (
        "receipt_line_corrections",
        "id",
        ("receipt_line_id", "correction_role", "correction_generation"),
    ),
    "RECEIPT_COMPARATOR": ("receipt_comparators", "id", ("receipt_id", "po_line_id")),
    "RECEIPT_RECONCILIATION": ("receipt_reconciliations", "id", ("receipt_id",)),
    "RECEIPT_CORRECTION_EVALUATION": (
        "receipt_correction_evaluations",
        "id",
        ("receipt_id", "evaluation_seq"),
    ),
    "RECEIPT_EVALUATION_LINE": (
        "receipt_evaluation_lines",
        "id",
        ("evaluation_id", "receipt_line_id"),
    ),
    "RECEIPT_EVALUATION_EXPECTATION": (
        "receipt_evaluation_expectations",
        "id",
        ("evaluation_id", "po_line_id"),
    ),
    "RECEIPT_EVALUATION_EXCEPTION": (
        "receipt_evaluation_exceptions",
        "id",
        ("evaluation_id", "exception_id", "supported"),
    ),
    "EXCEPTION_EVENT": ("exception_events", "id", ("exception_id", "to_status")),
    "SYNC_CONFLICT_EVENT": ("sync_conflict_events", "id", ("exception_id", "to_status")),
    "LABEL_TEMPLATE": ("label_templates", "id", ("name", "symbology")),
    "PRINT_JOB": ("print_jobs", "id", ("entity_id", "status")),
}


def resolve(connection, identifier):
    """Ambiguous administrative UUID collisions never silently select one entity."""
    found = []
    for entity, (name, key, fields) in SUMMARIES.items():
        table = getattr(db, name)
        summary = (
            connection.execute(
                select(*(table.c[field] for field in fields)).where(table.c[key] == identifier)
            )
            .mappings()
            .one_or_none()
        )
        if summary is not None:
            found.append(dict(entity_type=entity, entity_id=identifier, summary=dict(summary)))
    if not found:
        raise AssetNotFound("Identifier not found")
    if len(found) != 1:
        raise AssetConflict("Identifier is ambiguous")
    return found[0]
