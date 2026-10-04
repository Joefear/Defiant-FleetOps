"""Exception status derives from immutable events; observations never change."""

from uuid import UUID

from sqlalchemy import Connection, select, text
from uuid6 import uuid7

from fleetops.db.metadata import (
    exception_events,
    exception_workflows,
    receipt_lines,
    receipts,
    receiving_exceptions,
)
from fleetops.db.metadata import sync_conflicts as db_sync_conflicts
from fleetops.domain.assets import AssetInvalid, _constraints

WORKFLOW = {
    "OPEN": frozenset({"ACKNOWLEDGED", "RESOLVED", "WAIVED"}),
    "ACKNOWLEDGED": frozenset({"RESOLVED", "WAIVED"}),
    "RESOLVED": frozenset(),
    "WAIVED": frozenset(),
}


def transition_exception(connection: Connection, exception_id: UUID, *, values: dict):
    """The durable boundary locks before comparing expected status and derives the Actor."""
    if values["to_status"] not in WORKFLOW.get(values["expected_status"], ()):
        raise AssetInvalid("Illegal Exception workflow transition")
    from fleetops.capture import conflicts

    if (
        connection.execute(
            select(db_sync_conflicts.c.id).where(db_sync_conflicts.c.id == exception_id)
        ).scalar_one_or_none()
        is not None
    ):
        return conflicts.transition(connection, exception_id, values=values)
    with _constraints():
        return (
            connection.execute(
                text("""
          SELECT * FROM fleetops.transition_exception(:exception_id,:expected_status,
            :to_status,:note,:occurred_at,:event_id,NULL)
        """),
                dict(values, exception_id=exception_id, event_id=uuid7(), note=values.get("note")),
            )
            .mappings()
            .one()
        )


def get_exception(connection: Connection, exception_id: UUID):
    """Return immutable observation, maintained status and authoritative event sequence."""
    row = (
        connection.execute(
            select(
                receiving_exceptions,
                exception_workflows.c.severity,
                exception_workflows.c.entity_type,
                exception_workflows.c.entity_id,
                receiving_exceptions.c.actor_id.label("opened_by_actor_id"),
                receiving_exceptions.c.occurred_at.label("opened_at"),
                exception_workflows.c.status,
                exception_workflows.c.event_seq,
                exception_workflows.c.resolved_by_actor_id,
                exception_workflows.c.resolved_at,
                exception_workflows.c.resolution_note,
            )
            .join(
                exception_workflows, exception_workflows.c.exception_id == receiving_exceptions.c.id
            )
            .where(receiving_exceptions.c.id == exception_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        from fleetops.capture.conflicts import get_conflict

        return get_conflict(connection, exception_id)
    return dict(
        row,
        events=connection.execute(
            select(exception_events)
            .where(exception_events.c.exception_id == exception_id)
            .order_by(exception_events.c.event_seq)
        )
        .mappings()
        .all(),
    )


def list_open(
    connection: Connection,
    *,
    receipt_id=None,
    receipt_line_id=None,
    po_line_id=None,
    asset_id=None,
    entity_type=None,
    entity_id=None,
):
    """Filter explicit typed relationships inside the tenant boundary."""
    if (entity_type is None) != (entity_id is None):
        raise AssetInvalid("Supply both entity_type and entity_id")
    if entity_type == "ASSET":
        # A receiving UUID cannot become an Asset primary context merely because
        # sync conflicts share this endpoint. RLS keeps foreign and unknown IDs
        # indistinguishable; those ordinary filters still return an empty list.
        receiving_id = connection.execute(
            select(receipts.c.id)
            .where(receipts.c.id == entity_id)
            .union_all(select(receipt_lines.c.id).where(receipt_lines.c.id == entity_id))
            .limit(1)
        ).scalar_one_or_none()
        if receiving_id is not None:
            raise AssetInvalid("Receiving identifiers cannot name an ASSET primary context")
    query = (
        select(receiving_exceptions.c.id)
        .join(exception_workflows, exception_workflows.c.exception_id == receiving_exceptions.c.id)
        .where(exception_workflows.c.status.in_(["OPEN", "ACKNOWLEDGED"]))
    )
    for column, value in (
        (exception_workflows.c.entity_type, entity_type),
        (exception_workflows.c.entity_id, entity_id),
        (receiving_exceptions.c.receipt_id, receipt_id),
        (receiving_exceptions.c.receipt_line_id, receipt_line_id),
        (receiving_exceptions.c.po_line_id, po_line_id),
    ):
        if value is not None:
            query = query.where(column == value)
    if asset_id is not None:
        query = query.where(
            (receiving_exceptions.c.asset_id == asset_id)
            | (receiving_exceptions.c.conflicting_asset_id == asset_id)
        )
    from fleetops.capture import conflicts

    sync = conflicts.list_open(
        connection,
        receipt_id=receipt_id,
        receipt_line_id=receipt_line_id,
        po_line_id=po_line_id,
        asset_id=asset_id,
        entity_type=entity_type,
        entity_id=entity_id,
    )
    return sync + [
        get_exception(connection, identifier)
        for identifier in connection.execute(query.order_by(receiving_exceptions.c.id)).scalars()
    ]
