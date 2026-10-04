"""Authenticated evidence capture and link orchestration over ordinary tenant-scoped SQL."""

import hashlib
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import attachment_links, attachments
from fleetops.evidence.storage import EvidenceStorage, EvidenceStorageError, StoredEvidence


class EvidenceInvalid(Exception):
    """Unverifiable content, provenance or target; never expose SQL/storage diagnostics."""


class EvidenceNotFound(Exception):
    """Missing and foreign-tenant evidence share one public response."""


class EvidenceConflict(Exception):
    """An immutable prior capture cannot acquire a different supersession relationship."""


@contextmanager
def _constraints():
    """Leave rollback to the shared authenticated request transaction."""
    try:
        yield
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in {"23502", "23503", "23514", "42501"}:
            raise EvidenceInvalid("Invalid evidence provenance, target or attribution") from error
        if getattr(error.orig, "sqlstate", None) == "23505":
            raise EvidenceConflict("Evidence identity conflicts with an existing record") from error
        raise


def get_attachment(connection: Connection, attachment_id: UUID):
    """Resolve a tenant-visible UUID; a hash is never cross-tenant identity."""
    row = (
        connection.execute(select(attachments).where(attachments.c.id == attachment_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise EvidenceNotFound("Attachment not found")
    return row


def read_content(storage: EvidenceStorage | None, row) -> bytes:
    """Validate actual bytes before serving or relying on evidence in a workflow."""
    if storage is None:
        raise EvidenceStorageError("Evidence storage is not configured")
    return storage.read(
        row["org_id"], StoredEvidence(row["storage_key"], row["sha256"], row["byte_size"])
    )


def capture(
    connection: Connection, storage: EvidenceStorage, org_id: UUID, content: bytes, values: dict
):
    """Publish verified bytes before metadata, then deduplicate under a tenant-unique key.

    Publication has no overwrite/rollback operation. A DB failure can leave an
    unreachable content object but can never leave a committed row with an unfinished
    upload. A retry verifies and reuses that object. Concurrent DB INSERTs arbitrate
    through the unique constraint; the losing READ COMMITTED statement then reads the
    committed winner without changing that capture's provenance.
    """
    values = dict(values)
    supplied_hash = values.pop("sha256", None)
    digest = hashlib.sha256(content).hexdigest()
    if supplied_hash is not None and supplied_hash != digest:
        raise EvidenceInvalid("Supplied SHA-256 does not match uploaded bytes")
    parent = values.get("supersedes_attachment_id")
    if parent is not None:
        original = get_attachment(connection, parent)
        if original["sha256"] == digest:
            raise EvidenceInvalid("Supersession requires modified content")
        read_content(storage, original)
    stored = storage.put(org_id, content)
    with _constraints():
        row = (
            connection.execute(
                insert(attachments)
                .values(
                    id=uuid7(),
                    org_id=org_id,
                    sha256=digest,
                    byte_size=stored.byte_size,
                    storage_key=stored.key,
                    **values,
                )
                .on_conflict_do_nothing(constraint="uq_attachments_org_sha256")
                .returning(attachments)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        row = (
            connection.execute(
                select(attachments).where(
                    attachments.c.org_id == org_id, attachments.c.sha256 == digest
                )
            )
            .mappings()
            .one()
        )
        if parent is not None and row["supersedes_attachment_id"] != parent:
            raise EvidenceConflict("Existing content has different supersession provenance")
        read_content(storage, row)
    return row


def link(connection: Connection, storage: EvidenceStorage | None, attachment_id: UUID, values):
    """Append a typed link after verifying the captured bytes; DB validates target/Actor."""
    row = get_attachment(connection, attachment_id)
    read_content(storage, row)
    with _constraints():
        created = (
            connection.execute(
                insert(attachment_links)
                .values(
                    id=uuid7(),
                    org_id=row["org_id"],
                    attachment_id=attachment_id,
                    **values,
                )
                .on_conflict_do_nothing(constraint="uq_attachment_links_target_role")
                .returning(attachment_links)
            )
            .mappings()
            .one_or_none()
        )
        if created is None:
            created = (
                connection.execute(
                    select(attachment_links).where(
                        attachment_links.c.attachment_id == attachment_id,
                        *(attachment_links.c[key] == value for key, value in values.items()),
                    )
                )
                .mappings()
                .one()
            )
    return created


def list_links(connection: Connection, attachment_id: UUID):
    """Link history stays visible when a newer content object supersedes the attachment."""
    get_attachment(connection, attachment_id)
    return (
        connection.execute(
            select(attachment_links)
            .where(attachment_links.c.attachment_id == attachment_id)
            .order_by(attachment_links.c.id)
        )
        .mappings()
        .all()
    )


def verify_asset_evidence(
    connection: Connection,
    storage: EvidenceStorage | None,
    asset_id: UUID,
    attachment_id: UUID | None,
    *,
    role: str | None = None,
    required: bool = False,
):
    """Verify tenant, capture provenance, typed link and bytes; SQL rechecks the relationship.

    Link and attachment records are immutable for runtime callers. The later locked
    history INSERT independently enforces the relationship so direct SQL cannot replace
    it with a random UUID. Filesystem integrity is a service/adapter responsibility,
    outside PostgreSQL's privileges and transaction manager.
    """
    if attachment_id is None:
        if required:
            raise EvidenceInvalid("Verifiable disposal evidence is required")
        return
    row = get_attachment(connection, attachment_id)
    valid = connection.execute(
        text("""
        SELECT fleetops.valid_asset_evidence(:org,:attachment,:asset,:role)
    """),
        dict(org=row["org_id"], attachment=attachment_id, asset=asset_id, role=role),
    ).scalar_one()
    if not valid:
        raise EvidenceInvalid("Evidence provenance or Asset link is invalid")
    read_content(storage, row)
