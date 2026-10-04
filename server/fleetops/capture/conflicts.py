"""Sync observations retain their own origin instead of fabricating a receipt."""

from sqlalchemy import select, text
from uuid6 import uuid7

from fleetops.db.metadata import sync_conflict_events, sync_conflicts
from fleetops.domain.assets import AssetNotFound, _constraints


def get_conflict(connection, exception_id, *, lock=False):
    """Derive status from events; optional advisory locking needs no history UPDATE grant."""
    query = select(sync_conflicts).where(sync_conflicts.c.id == exception_id)
    if lock:
        connection.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended("
                "'sync-conflict:' || CAST(:id AS text),0))"
            ),
            {"id": exception_id},
        )
    row = connection.execute(query).mappings().one_or_none()
    if row is None:
        raise AssetNotFound("Exception not found")
    events = [
        dict(event, evaluation_id=None)
        for event in connection.execute(
            select(sync_conflict_events)
            .where(sync_conflict_events.c.exception_id == exception_id)
            .order_by(sync_conflict_events.c.event_seq)
        ).mappings()
    ]
    latest = events[-1] if events else None
    terminal = latest if latest and latest["to_status"] in ("RESOLVED", "WAIVED") else None
    return dict(
        row,
        exception_type="SYNC_CONFLICT",
        severity="UNSPECIFIED",
        entity_type="ASSET",
        entity_id=row["asset_id"],
        opened_by_actor_id=row["actor_id"],
        opened_at=row["occurred_at"],
        status=latest["to_status"] if latest else "OPEN",
        event_seq=latest["event_seq"] if latest else 0,
        resolved_by_actor_id=terminal["actor_id"] if terminal else None,
        resolved_at=terminal["occurred_at"] if terminal else None,
        resolution_note=terminal["note"] if terminal else None,
        events=events,
    )


def transition(connection, exception_id, *, values):
    """Append one legal expected-status change under the per-conflict advisory lock."""
    # Keep the origin lock across the history read and insert. The trigger repeats
    # this protocol for direct runtime SQL and rejects stale or terminal changes.
    current = get_conflict(connection, exception_id, lock=True)
    with _constraints():
        row = (
            connection.execute(
                sync_conflict_events.insert()
                .values(
                    id=uuid7(),
                    org_id=current["org_id"],
                    exception_id=exception_id,
                    event_seq=current["event_seq"] + 1,
                    from_status=values["expected_status"],
                    to_status=values["to_status"],
                    note=values.get("note"),
                    occurred_at=values["occurred_at"],
                )
                .returning(sync_conflict_events)
            )
            .mappings()
            .one()
        )
    return dict(row, evaluation_id=None)


def list_open(connection, **filters):
    """Filter unresolved Asset conflicts without treating them as receiving facts."""
    if any(filters.get(key) is not None for key in ("receipt_id", "receipt_line_id", "po_line_id")):
        return []
    if filters.get("entity_type") not in (None, "ASSET"):
        return []
    query = select(sync_conflicts.c.id)
    for value in (filters.get("asset_id"), filters.get("entity_id")):
        if value is not None:
            query = query.where(sync_conflicts.c.asset_id == value)
    rows = [
        get_conflict(connection, identifier)
        for identifier in connection.execute(query.order_by(sync_conflicts.c.id)).scalars()
    ]
    return [row for row in rows if row["status"] in ("OPEN", "ACKNOWLEDGED")]
