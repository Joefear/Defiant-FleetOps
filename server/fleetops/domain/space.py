"""Create/list space and resolve actual ancestry within the caller's RLS transaction."""

from contextlib import contextmanager
from functools import cache
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from sqlalchemy import Connection, select, text
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from fleetops.db.metadata import facilities, locations


class SpaceConflict(Exception):
    """A facility-local code already exists."""


class SpaceInvalid(Exception):
    """A space value or tenant/facility relationship is invalid."""


class SpaceNotFound(Exception):
    """The requested location is missing or invisible under RLS."""


class SpaceHierarchyInvalid(Exception):
    """Stored ancestry is cyclic or incomplete; returning a partial path would lie."""


@cache
def _iana_zones() -> frozenset[str]:
    """Read local zoneinfo/tzdata once; never fetch timezone data during a request."""
    return frozenset(available_timezones())


def validate_timezone(value: str) -> str:
    """Require a real IANA key, excluding file paths and special POSIX/right databases.

    Facility local time is metadata. It never changes timestamp storage or the
    connection timezone. tzdata supplies a portable local fallback on Windows.
    """
    try:
        if value not in _iana_zones():
            raise ValueError("Unknown IANA timezone")
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown IANA timezone") from error
    return value


@contextmanager
def _space_constraints():
    """Hide database relationship details while allowing the request to roll back."""
    try:
        yield
    except IntegrityError as error:
        if error.orig.sqlstate == "23505":
            raise SpaceConflict("Duplicate location code in facility") from error
        if error.orig.sqlstate in {"23503", "23514", "23502"}:
            raise SpaceInvalid("Invalid space value or relationship") from error
        raise


def create_facility(connection: Connection, *, org_id: UUID, performer_id: UUID, values: dict):
    """Assign identity and attribution server-side and validate locally before writing."""
    try:
        validate_timezone(values["timezone"])
    except ValueError as error:
        raise SpaceInvalid(str(error)) from error
    with _space_constraints():
        return (
            connection.execute(
                facilities.insert()
                .values(**values, id=uuid7(), org_id=org_id, created_by_actor_id=performer_id)
                .returning(facilities)
            )
            .mappings()
            .one()
        )


def list_facilities(connection: Connection):
    """List visible facilities, retaining inactive entries without lifecycle assumptions."""
    return connection.execute(select(facilities).order_by(facilities.c.id)).mappings().all()


def create_location(connection: Connection, *, org_id: UUID, performer_id: UUID, values: dict):
    """Create one fresh UUID beneath an existing parent; no parent-changing path exists.

    The composite FKs enforce tenant/facility membership. Server-assigned IDs and
    insert-only operations cannot link an existing ancestor back beneath a new child.
    PostgreSQL rejects direct self-parenting and uses an AFTER INSERT ancestry
    guard to reject deeper cycles, including cycles submitted in one SQL batch.
    """
    with _space_constraints():
        return (
            connection.execute(
                locations.insert()
                .values(**values, id=uuid7(), org_id=org_id, created_by_actor_id=performer_id)
                .returning(locations)
            )
            .mappings()
            .one()
        )


def list_locations(connection: Connection):
    """List locations through RLS, without assuming that any physical thing occupies them."""
    return connection.execute(select(locations).order_by(locations.c.id)).mappings().all()


def location_path(connection: Connection, location_id: UUID):
    """Return complete root-to-target ancestry from one PostgreSQL statement snapshot.

    Explicit tenant/facility joins and runtime RLS protect every recursive step.
    A visited-ID array stops corrupt cycles without an arbitrary depth truncation.
    An unresolved parent is an error, never a fabricated root. No privileged lookup
    is used even when corruption or an invisible ancestor explains the missing link.
    """
    rows = (
        connection.execute(
            text("""
            WITH RECURSIVE ancestry AS (
                SELECT l.*, 0 AS depth, ARRAY[l.id] AS visited, false AS is_cycle
                FROM fleetops.locations l WHERE l.id = :location_id
                UNION ALL
                SELECT p.*, a.depth + 1, a.visited || p.id, p.id = ANY(a.visited)
                FROM ancestry a
                JOIN fleetops.locations p
                  ON p.id = a.parent_location_id
                 AND p.org_id = a.org_id AND p.facility_id = a.facility_id
                WHERE NOT a.is_cycle
            )
            SELECT * FROM ancestry ORDER BY depth DESC
        """),
            {"location_id": location_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        raise SpaceNotFound("Location not found")
    if any(row["is_cycle"] for row in rows) or rows[0]["parent_location_id"] is not None:
        raise SpaceHierarchyInvalid("Location ancestry is cyclic or incomplete")
    return [{column.name: row[column.name] for column in locations.columns} for row in rows]
