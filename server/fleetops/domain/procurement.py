"""Procurement authoring and immutable expectation history under ADR-009.

Python coordinates bounded workflows. Invoker triggers and constraints independently
enforce the same admission rules for direct runtime SQL and ordinary migrator writes.
"""

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, exists, select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders


class ProcurementNotFound(Exception):
    """Missing and invisible tenant targets share one public result."""


class ProcurementConflict(Exception):
    """The selected expectation or authoring state is no longer admissible."""


class ProcurementInvalid(Exception):
    """A business input or required relationship violates the durable contract."""


@contextmanager
def constraints():
    """Hide database values and let the request transaction roll back on any failure."""
    try:
        yield
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", None)
        if state in {"23505", "40001", "40P01"}:
            raise ProcurementConflict(
                "Purchase order or line conflicts with current history"
            ) from error
        if state in {"23502", "23503", "23514", "22003", "22008"}:
            raise ProcurementInvalid("Invalid procurement value or relationship") from error
        raise


def get_order(connection: Connection, po_id: UUID, *, lock: bool = False):
    """Use RLS for visibility; acquire the shared PO lock before workflow decisions."""
    query = select(orders).where(orders.c.id == po_id)
    if lock:
        query = query.with_for_update(key_share=True)
    row = connection.execute(query).mappings().one_or_none()
    if row is None:
        raise ProcurementNotFound("Purchase order not found")
    return dict(row)


def require_state(order, state: str) -> None:
    """An enum value is not permission to introduce another procurement workflow."""
    if order["status"] != state:
        raise ProcurementConflict(f"Purchase order must be {state}")


def create_order(connection: Connection, *, org_id: UUID, performer_id: UUID, values: dict):
    """Assign opaque identity; duplicate display numbers do not replace UUID identity."""
    with constraints():
        return (
            connection.execute(
                orders.insert()
                .values(
                    **values,
                    id=uuid7(),
                    org_id=org_id,
                    created_by_actor_id=performer_id,
                    updated_by_actor_id=performer_id,
                )
                .returning(orders)
            )
            .mappings()
            .one()
        )


def list_orders(connection: Connection):
    """List only the authenticated tenant's recorded procurement expectations."""
    return connection.execute(select(orders).order_by(orders.c.id)).mappings().all()


def update_order(connection: Connection, po_id: UUID, *, values: dict):
    """Draft metadata changes retain original identity and database-bound updater time."""
    with constraints():
        order = get_order(connection, po_id, lock=True)
        require_state(order, "DRAFT")
        if not values:
            return order
        return (
            connection.execute(
                orders.update().where(orders.c.id == po_id).values(**values).returning(orders)
            )
            .mappings()
            .one()
        )


def issue_order(connection: Connection, po_id: UUID):
    """Freeze through the PO state in the same transaction that sets issuance attribution.

    The handoff supplies no minimum-line approval workflow; an empty draft may be
    issued. Every existing line already satisfies its durable input constraints.
    """
    with constraints():
        order = get_order(connection, po_id, lock=True)
        require_state(order, "DRAFT")
        return (
            connection.execute(
                orders.update()
                .where(orders.c.id == po_id)
                .values(status="ISSUED")
                .returning(orders)
            )
            .mappings()
            .one()
        )


def line_query():
    """Active is successor absence in the visible snapshot, never a mutable marker."""
    successor = lines.alias("successor")
    superseded = exists(
        select(successor.c.id).where(
            successor.c.org_id == lines.c.org_id,
            successor.c.po_id == lines.c.po_id,
            successor.c.supersedes_line_id == lines.c.id,
        )
    ).correlate(lines)
    return select(lines, (~superseded).label("active"), superseded.label("superseded"))


def get_line(connection: Connection, po_id: UUID, line_id: UUID):
    """A line UUID must resolve in this PO as well as in the authenticated tenant."""
    row = (
        connection.execute(
            line_query().where(
                lines.c.po_id == po_id,
                lines.c.id == line_id,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ProcurementNotFound("Purchase order line not found")
    return dict(row)


def list_lines(connection: Connection, po_id: UUID):
    """Return every version, ordered by logical line then root-to-leaf pointers.

    Timestamps and UUIDs identify neither the active expectation nor its position
    in a lineage. Defensive checks expose corrupt history rather than omitting it.
    """
    get_order(connection, po_id)
    rows = [
        dict(row)
        for row in connection.execute(line_query().where(lines.c.po_id == po_id)).mappings()
    ]
    successors = {}
    roots = {}
    for row in rows:
        prior = row["supersedes_line_id"]
        mapping, key = (roots, row["line_number"]) if prior is None else (successors, prior)
        if key in mapping:
            raise ProcurementConflict("Inconsistent procurement lineage")
        mapping[key] = row
    ordered, seen = [], set()
    for number in sorted(roots):
        row = roots[number]
        while row is not None:
            if row["id"] in seen or row["line_number"] != number:
                raise ProcurementConflict("Inconsistent procurement lineage")
            seen.add(row["id"])
            ordered.append(row)
            row = successors.get(row["id"])
    if len(ordered) != len(rows):
        raise ProcurementConflict("Inconsistent procurement lineage")
    return ordered


def create_line(
    connection: Connection,
    po_id: UUID,
    *,
    performer_id: UUID,
    values: dict,
    predecessor_id: UUID | None = None,
):
    """Append a root or successor; the database copies this version's Item UOM once."""
    with constraints():
        order = get_order(connection, po_id, lock=True)
        require_state(order, "DRAFT" if predecessor_id is None else "ISSUED")
        if predecessor_id is not None:
            prior = get_line(connection, po_id, predecessor_id)
            if not prior["active"]:
                raise ProcurementConflict("Predecessor is no longer active")
            values = values | {"line_number": prior["line_number"]}
        line_id = uuid7()
        connection.execute(
            lines.insert().values(
                **values,
                id=line_id,
                org_id=order["org_id"],
                po_id=po_id,
                supersedes_line_id=predecessor_id,
                created_by_actor_id=performer_id,
                updated_by_actor_id=performer_id,
            )
        )
        return get_line(connection, po_id, line_id)


def update_line(connection: Connection, po_id: UUID, line_id: UUID, *, values: dict):
    """Draft edits preserve the original UOM snapshot even if the selected Item changes."""
    with constraints():
        order = get_order(connection, po_id, lock=True)
        require_state(order, "DRAFT")
        get_line(connection, po_id, line_id)
        if values:
            connection.execute(
                lines.update().where(lines.c.po_id == po_id, lines.c.id == line_id).values(**values)
            )
        return get_line(connection, po_id, line_id)
