"""One-snapshot audit reconstruction; occurrence order never supplies fact authority."""

from uuid import UUID

from sqlalchemy import Connection, text

from fleetops.domain.assets import AssetNotFound


def _event(
    kind,
    table,
    predicate,
    *,
    actor="actor_id",
    occurred="occurred_at",
    recorded="recorded_at",
    identity="id",
    source="claimed",
    extra="'{}'::jsonb",
):
    """Compile fixed internal branches, never a caller-selected table or expression.

    Historical NUMERIC values travel as decimal strings, avoiding the loss of price
    precision that ordinary JSON number decoding would introduce. Storage locations,
    transaction IDs and the cross-tenant configuration sequence are not public facts.
    """
    return f"""
      SELECT '{kind}'::text AS kind,
        h.{identity} AS id, h.{actor} AS actor_id,
        h.{occurred} AS occurred_at, h.{recorded} AS recorded_at,
        '{source}'::text AS occurred_at_source,
        (to_jsonb(h)->>'result_version')::integer AS result_version,
        (to_jsonb(h) - ARRAY['org_id','configuration_seq','recording_transaction_id',
                            'storage_key','payload'])
          || CASE WHEN to_jsonb(h) ? 'quantity'
                  THEN jsonb_build_object('quantity',to_jsonb(h)->>'quantity')
                  ELSE '{{}}'::jsonb END
          || CASE WHEN to_jsonb(h) ? 'unit_price'
                  THEN jsonb_build_object('unit_price',to_jsonb(h)->>'unit_price')
                  ELSE '{{}}'::jsonb END
          || CASE WHEN to_jsonb(h) ? 'packing_quantity'
                  THEN jsonb_build_object('packing_quantity',to_jsonb(h)->>'packing_quantity')
                  ELSE '{{}}'::jsonb END
          || {extra} AS facts
      FROM {table} h WHERE {predicate}
    """


_ASSET = "h.asset_id IN (SELECT id FROM unit)"
_BRANCHES = [
    _event(kind, "fleetops." + table, _ASSET)
    for kind, table in (
        ("TRANSITION", "asset_transitions"),
        ("MOVEMENT", "asset_movements"),
        ("CUSTODY", "asset_custody_changes"),
        ("OWNERSHIP", "asset_ownership_changes"),
        ("ASSIGNMENT", "asset_assignment_events"),
    )
]
_BRANCHES += [
    _event("INITIAL_FACTS", "fleetops.asset_initial_facts", _ASSET, identity="asset_id"),
    _event(
        "INITIAL_ASSIGNMENT", "fleetops.asset_initial_assignment_facts", _ASSET, identity="asset_id"
    ),
    _event(
        "IDENTIFIER",
        "fleetops.asset_identifiers",
        _ASSET,
        actor="created_by_actor_id",
        occurred="created_at",
        recorded="created_at",
        source="recorded_creation",
    ),
    _event(
        "CONFIGURATION",
        "fleetops.asset_configurations",
        _ASSET,
        actor="applied_by",
        occurred="applied_at",
    ),
    _event("RECEIPT_LINE", "unit_lines", "true"),
    _event(
        "RECEIPT",
        "fleetops.receipts",
        "h.id IN (SELECT receipt_id FROM unit_lines)",
        extra="""jsonb_build_object('vendor',
                jsonb_build_object('id',
                h.vendor_party_id,
                'display_name',
                (SELECT p.display_name
                FROM fleetops.parties p
                WHERE p.id=h.vendor_party_id)))
        """,
    ),
    _event(
        "RECEIPT_COMPARATOR",
        "unit_comparators",
        "true",
        extra="""jsonb_build_object('expected_line',
                COALESCE((SELECT jsonb_build_object('id',
                p.id,
                'po_id',
                p.po_id,
                'item_id',
                p.item_id,
                'quantity',
                p.quantity::text,
                'uom',
                p.uom,
                'unit_price',
                p.unit_price::text)
                FROM fleetops.purchase_order_lines p
                WHERE p.id=h.po_line_id AND h.source_generation=0),
                (SELECT jsonb_build_object('id',
                p.id,
                'po_id',
                p.po_id,
                'item_id',
                p.item_id,
                'quantity',
                p.quantity::text,
                'uom',
                p.uom,
                'unit_price',
                p.unit_price::text)
                FROM fleetops.purchase_order_line_corrections p
                WHERE p.id=h.source_id)))
        """,
    ),
    _event(
        "PROCUREMENT_LINE",
        "fleetops.purchase_order_lines",
        "h.id IN (SELECT po_line_id FROM unit_comparators)",
        actor="created_by_actor_id",
        occurred="created_at",
        recorded="created_at",
        source="recorded_creation",
    ),
    _event("RECEIPT_CORRECTION", "unit_corrections", "true"),
    _event(
        "PROCUREMENT_CORRECTION",
        "fleetops.purchase_order_line_corrections",
        "h.po_line_id IN (SELECT po_line_id FROM unit_comparators)",
    ),
    _event("EXCEPTION", "unit_exceptions", "true"),
    _event(
        "EXCEPTION_EVENT",
        "fleetops.exception_events",
        "h.exception_id IN (SELECT id FROM unit_exceptions)",
    ),
    _event("SYNC_CONFLICT", "fleetops.sync_conflicts", _ASSET),
    _event(
        "SYNC_CONFLICT_EVENT",
        "fleetops.sync_conflict_events",
        "h.exception_id IN (SELECT id FROM fleetops.sync_conflicts "
        "WHERE asset_id IN (SELECT id FROM unit))",
    ),
    _event(
        "ATTACHMENT",
        "fleetops.attachments",
        "h.id IN (SELECT attachment_id FROM unit_links)",
        actor="captured_by",
        occurred="captured_at",
    ),
    _event("ATTACHMENT_LINK", "unit_links", "true", occurred="recorded_at", source="recorded_link"),
]

HISTORY_SQL = text(
    """
WITH unit AS (SELECT * FROM fleetops.assets WHERE id=:asset_id),
unit_lines AS (
    SELECT l.* FROM fleetops.receipt_lines l WHERE l.asset_id IN (SELECT id FROM unit)
    OR EXISTS (SELECT 1 FROM fleetops.receipt_line_corrections c
               WHERE c.receipt_line_id=l.id AND c.correction_role='CORRECTED'
                 AND (c.asset_id IN (SELECT id FROM unit)
                      OR c.conflicting_asset_id IN (SELECT id FROM unit)))
    OR EXISTS (SELECT 1 FROM fleetops.receiving_exceptions e
               WHERE e.receipt_line_id=l.id AND e.conflicting_asset_id IN (SELECT id FROM unit))
), unit_corrections AS (
    SELECT c.* FROM fleetops.receipt_line_corrections c
    WHERE c.receipt_line_id IN (SELECT id FROM unit_lines)
), unit_comparators AS (
    SELECT c.* FROM fleetops.receipt_comparators c
    WHERE EXISTS (SELECT 1 FROM unit_lines l
                  WHERE l.receipt_id=c.receipt_id AND l.po_line_id=c.po_line_id)
       OR EXISTS (SELECT 1 FROM unit_corrections l
                  WHERE l.receipt_id=c.receipt_id AND l.po_line_id=c.po_line_id)
), unit_exceptions AS (
    SELECT e.* FROM fleetops.receiving_exceptions e
    WHERE e.asset_id IN (SELECT id FROM unit) OR e.conflicting_asset_id IN (SELECT id FROM unit)
       OR e.receipt_line_id IN (SELECT id FROM unit_lines)
       OR (e.receipt_line_id IS NULL AND EXISTS (
           SELECT 1 FROM unit_comparators c WHERE c.receipt_id=e.receipt_id
                                              AND c.po_line_id=e.po_line_id))
       OR (e.receipt_line_id IS NULL AND e.po_line_id IS NULL
           AND e.receipt_id IN (SELECT receipt_id FROM unit_lines))
), unit_links AS (
    SELECT l.* FROM fleetops.attachment_links l
    WHERE (l.entity_type='ASSET' AND l.entity_id IN (SELECT id FROM unit))
       OR (l.entity_type='RECEIPT' AND l.entity_id IN (SELECT receipt_id FROM unit_lines))
       OR (l.entity_type='RECEIPT_LINE' AND l.entity_id IN (SELECT id FROM unit_lines))
       OR (l.entity_type='ASSET_CONFIGURATION' AND l.entity_id IN (
           SELECT id FROM fleetops.asset_configurations WHERE asset_id IN (SELECT id FROM unit)))
       OR (l.entity_type='RECEIVING_EXCEPTION' AND l.entity_id IN (SELECT id FROM unit_exceptions))
), events AS (
"""
    + "\nUNION ALL\n".join(_BRANCHES)
    + """
)
SELECT (SELECT to_jsonb(unit) FROM unit) AS asset,
       COALESCE((SELECT jsonb_agg(to_jsonb(e) ORDER BY occurred_at,recorded_at,kind,id)
                 FROM events e),'[]'::jsonb) AS entries,
       COALESCE((SELECT jsonb_agg(c.id ORDER BY c.configuration_seq)
                 FROM fleetops.asset_configurations c
                 WHERE c.asset_id IN (SELECT id FROM unit)),
                '[]'::jsonb) AS configuration_order
"""
)


def asset_history(connection: Connection, asset_id: UUID):
    """Read raw evidence including cancelled generations; never silently repair it.

    Sort ties deterministically for presentation only. Produced result_version and
    correction pointers remain in the response for authoritative reconstruction.
    Configuration IDs separately retain visible allocation order in this snapshot;
    their last ID is current, without exposing the shared sequence value.
    An attachment link has no separate occurrence claim in the accepted schema;
    its recorded instant is explicitly labelled, while capture time/Actor live in
    the independent ATTACHMENT entry. Missing receipt/baseline facts stay missing.
    """
    result = connection.execute(HISTORY_SQL, {"asset_id": asset_id}).mappings().one()
    if result["asset"] is None:
        raise AssetNotFound("Asset not found")
    entries = result["entries"]
    corrections = {}
    for entry in entries:
        facts = entry["facts"]
        if facts.get("correction_role") == "CORRECTED":
            root = _root(facts)
            corrections[(entry["kind"], root, facts["correction_generation"])] = entry["id"]
    for entry in entries:
        facts = entry["facts"]
        entry["cancels_history_id"] = None
        if facts.get("correction_role") == "REVERSAL":
            root = _root(facts)
            generation = facts["correction_generation"]
            entry["cancels_history_id"] = (
                root if generation == 1 else corrections.get((entry["kind"], root, generation - 1))
            )
    return {
        "asset": result["asset"],
        "entries": entries,
        "configuration_order": result["configuration_order"],
    }


def _root(facts):
    """Keep administrative cancellation identity separate from an event's target."""
    return (
        next((value for key, value in facts.items() if key.startswith("corrects_")), None)
        or facts.get("receipt_line_id")
        or facts.get("po_line_id")
    )
