"""Catalog persistence and external-reference searches under the caller's trusted RLS transaction.

Opaque IDs address entities. External identifiers are search data and may return several
targets; no write resolves one by guessing which match the caller probably intended.
"""

from contextlib import contextmanager
from types import MappingProxyType
from uuid import UUID

from sqlalchemy import Connection, func, select
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from fleetops.db.metadata import external_references, items, parties
from fleetops.domain.reference_types import ReferenceEntityType

REFERENCE_TARGETS = MappingProxyType(
    {
        ReferenceEntityType.ITEM: items,
        ReferenceEntityType.PARTY: parties,
    }
)


class CatalogConflict(Exception):
    """A duplicate catalog entry or repeated attachment was rejected by PostgreSQL."""


class CatalogInvalid(Exception):
    """A catalog value or required relationship violates a database constraint."""


class CatalogNotFound(Exception):
    """A target does not exist or is invisible to the authenticated tenant."""


@contextmanager
def _catalog_constraints():
    """Translate expected integrity failures without exposing another tenant's values.

    The exception propagates through the request transaction, which rolls back; an
    aborted database transaction must never be caught and then committed as success.
    """
    try:
        yield
    except IntegrityError as error:
        if error.orig.sqlstate == "23505":
            raise CatalogConflict("Duplicate catalog entry or reference attachment") from error
        if error.orig.sqlstate in {"23503", "23514", "23502"}:
            raise CatalogInvalid("Invalid catalog value or manufacturer relationship") from error
        raise


def create_item(connection: Connection, *, org_id: UUID, performer_id: UUID, values: dict):
    """Assign server-owned identity and attribution; the database enforces catalog constraints."""
    with _catalog_constraints():
        return (
            connection.execute(
                items.insert()
                .values(
                    **values,
                    id=uuid7(),
                    org_id=org_id,
                    created_by_actor_id=performer_id,
                    updated_by_actor_id=performer_id,
                )
                .returning(items)
            )
            .mappings()
            .one()
        )


def get_item(connection: Connection, item_id: UUID):
    """Cross-tenant and missing IDs have the same not-found result under RLS."""
    row = connection.execute(select(items).where(items.c.id == item_id)).mappings().one_or_none()
    if row is None:
        raise CatalogNotFound("Item not found")
    return row


def list_items(connection: Connection):
    """Return catalog entries visible to this transaction, including inactive entries."""
    return connection.execute(select(items).order_by(items.c.id)).mappings().all()


def update_item(connection: Connection, item_id: UUID, *, performer_id: UUID, values: dict):
    """Update descriptive/default fields by opaque ID; this is not transactional history.

    The API supplies only mutable fields. Runtime column grants separately protect
    identity, organization, and creation attribution from accidental direct SQL updates.
    """
    if not values:
        return get_item(connection, item_id)
    with _catalog_constraints():
        row = (
            connection.execute(
                items.update()
                .where(items.c.id == item_id)
                .values(
                    **values,
                    updated_by_actor_id=performer_id,
                    updated_at=func.statement_timestamp(),
                )
                .returning(items)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise CatalogNotFound("Item not found")
    return row


def validate_reference_target(
    connection: Connection, entity_type: ReferenceEntityType, entity_id: UUID
):
    """Use only the runtime connection under trusted RLS, never privileged target visibility.

    The small registry binds supported names to actual tables without dynamic SQL or
    future-domain placeholders. Both existence and tenant visibility are established
    before attaching. No runtime physical-delete path exists for ITEM or PARTY.
    """
    target = REFERENCE_TARGETS.get(entity_type)
    if target is None:
        raise CatalogInvalid("Unsupported reference entity type")
    if (
        connection.execute(select(target.c.id).where(target.c.id == entity_id)).scalar_one_or_none()
        is None
    ):
        raise CatalogNotFound("Reference target not found")


def attach_reference(
    connection: Connection,
    *,
    org_id: UUID,
    performer_id: UUID,
    entity_type: ReferenceEntityType,
    entity_id: UUID,
    values: dict,
):
    """An explicit FleetOps target is mandatory even when an external value looks unique."""
    validate_reference_target(connection, entity_type, entity_id)
    with _catalog_constraints():
        return (
            connection.execute(
                external_references.insert()
                .values(
                    **values,
                    id=uuid7(),
                    org_id=org_id,
                    created_by_actor_id=performer_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                )
                .returning(external_references)
            )
            .mappings()
            .one()
        )


def list_references(connection: Connection, entity_type: ReferenceEntityType, entity_id: UUID):
    """List attachments only after checking the explicit target in the same tenant context."""
    validate_reference_target(connection, entity_type, entity_id)
    return (
        connection.execute(
            select(external_references)
            .where(
                external_references.c.entity_type == entity_type,
                external_references.c.entity_id == entity_id,
            )
            .order_by(external_references.c.id)
        )
        .mappings()
        .all()
    )


def search_references(
    connection: Connection, *, system: str, reference_type: str, external_value: str
):
    """Return every match under RLS with opaque target IDs, preserving ADR-003 cardinality."""
    return (
        connection.execute(
            select(external_references)
            .where(
                external_references.c.system == system,
                external_references.c.reference_type == reference_type,
                external_references.c.external_value == external_value,
            )
            .order_by(external_references.c.entity_type, external_references.c.entity_id)
        )
        .mappings()
        .all()
    )
