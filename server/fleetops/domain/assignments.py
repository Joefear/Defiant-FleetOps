"""Immutable assignment events and derived intervals; no projection supplies historical truth."""

from uuid import UUID

from sqlalchemy import Connection, select, text
from uuid6 import uuid7

from fleetops.db.metadata import asset_assignment_events, asset_initial_assignment_facts
from fleetops.domain.assets import _constraints, get_asset


def assign_asset(connection: Connection, asset_id: UUID, *, values: dict):
    """The atomic SQL boundary derives first assignment versus reassignment from history."""
    with _constraints():
        return (
            connection.execute(
                text("""
            SELECT * FROM fleetops.assign_asset(
                :asset_id, :expected_version, :assignee_type, :assignee_id,
                :reason, :occurred_at, NULL, :event_id)
        """),
                dict(values, asset_id=asset_id, event_id=uuid7()),
            )
            .mappings()
            .one()
        )


def unassign_asset(connection: Connection, asset_id: UUID, *, values: dict):
    """End the current assignment with one new event, never by editing its establishing row."""
    with _constraints():
        return (
            connection.execute(
                text("""
            SELECT * FROM fleetops.unassign_asset(
                :asset_id, :expected_version, :reason, :occurred_at, NULL, :event_id)
        """),
                dict(values, asset_id=asset_id, event_id=uuid7()),
            )
            .mappings()
            .one()
        )


def assignment_history(connection: Connection, asset_id: UUID):
    """Reconstruct from the witness and events; Asset lookup checks visibility only.

    Successor order is produced-version order even when clocks disagree. Filtering out
    UNASSIGN before finding successors would silently leave its preceding interval open.
    """
    get_asset(connection, asset_id)
    witness = (
        connection.execute(
            select(asset_initial_assignment_facts).where(
                asset_initial_assignment_facts.c.asset_id == asset_id
            )
        )
        .mappings()
        .one_or_none()
    )
    events = (
        connection.execute(
            select(asset_assignment_events)
            .where(asset_assignment_events.c.asset_id == asset_id)
            .order_by(asset_assignment_events.c.result_version)
        )
        .mappings()
        .all()
    )
    intervals = []
    for index, event in enumerate(events):
        if event["to_assignee_id"] is not None:
            intervals.append(
                dict(
                    establishing_event_id=event["id"],
                    assignee_type=event["to_assignee_type"],
                    assignee_id=event["to_assignee_id"],
                    started_at=event["occurred_at"],
                    ended_at=events[index + 1]["occurred_at"] if index + 1 < len(events) else None,
                )
            )
    return dict(initial_assignment_fact=witness, events=events, intervals=intervals)
