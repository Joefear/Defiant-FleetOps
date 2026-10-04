"""Current D17 attachment metadata; UUID identity is distinct from content deduplication."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from fleetops.evidence.types import EvidenceEntity, EvidenceRole, EvidenceSource


def define_evidence_tables(metadata):
    """Scope every identity, hash and reference to its organization (D18).

    Duplicate bytes reuse the first immutable capture record within one tenant. A
    different tenant receives a different UUID, provenance and physical storage key.
    Links are append-only observations with their own authenticated capture attribution.
    """

    def relation(name, *columns):
        return Table(
            name,
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("org_id", Uuid, nullable=False),
            *columns,
            UniqueConstraint("org_id", "id", name=f"uq_{name}_org_id"),
            ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name=f"fk_{name}_org"),
        )

    def reference(table, column, target):
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", column],
                [f"fleetops.{target}.org_id", f"fleetops.{target}.id"],
                name=f"fk_{table.name}_{column}",
            )
        )
        Index(f"ix_{table.name}_{column}", table.c.org_id, table.c[column])

    def choices(column, values, name):
        allowed = ", ".join(f"'{value}'" for value in values)
        return CheckConstraint(f"{column} IN ({allowed})", name=name)

    attachments = relation(
        "attachments",
        Column("sha256", Text, nullable=False),
        Column("byte_size", BigInteger, nullable=False),
        Column("media_type", Text, nullable=False),
        Column("storage_key", Text, nullable=False),
        Column("source_type", Text, nullable=False),
        Column(
            "captured_by",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column("captured_at", DateTime(timezone=True), nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        Column("supersedes_attachment_id", Uuid),
        Column("original_filename", Text, nullable=False),
        UniqueConstraint("org_id", "sha256", name="uq_attachments_org_sha256"),
        UniqueConstraint("org_id", "storage_key", name="uq_attachments_org_storage"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_attachments_sha256"),
        CheckConstraint("byte_size >= 0", name="ck_attachments_byte_size"),
        CheckConstraint(
            "storage_key = org_id::text || '/' || sha256", name="ck_attachments_storage_key"
        ),
        CheckConstraint("isfinite(captured_at)", name="ck_attachments_captured_at"),
        CheckConstraint("isfinite(recorded_at)", name="ck_attachments_recorded_at"),
        CheckConstraint(
            "supersedes_attachment_id IS NULL OR supersedes_attachment_id <> id",
            name="ck_attachments_not_self_superseding",
        ),
        CheckConstraint(
            "length(btrim(original_filename)) BETWEEN 1 AND 255",
            name="ck_attachments_filename",
        ),
        CheckConstraint(
            "media_type ~ '^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$' "
            "AND length(media_type) <= 127",
            name="ck_attachments_media_type",
        ),
        choices("source_type", EvidenceSource, "ck_attachments_source_type"),
    )
    reference(attachments, "captured_by", "actors")
    reference(attachments, "supersedes_attachment_id", "attachments")
    links = relation(
        "attachment_links",
        Column("attachment_id", Uuid, nullable=False),
        Column("entity_type", Text, nullable=False),
        Column("entity_id", Uuid, nullable=False),
        Column("link_role", Text, nullable=False),
        Column(
            "actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        UniqueConstraint(
            "org_id",
            "attachment_id",
            "entity_type",
            "entity_id",
            "link_role",
            name="uq_attachment_links_target_role",
        ),
        choices("entity_type", EvidenceEntity, "ck_attachment_links_entity_type"),
        choices("link_role", EvidenceRole, "ck_attachment_links_role"),
        CheckConstraint("isfinite(recorded_at)", name="ck_attachment_links_recorded_at"),
    )
    reference(links, "attachment_id", "attachments")
    reference(links, "actor_id", "actors")
    Index("ix_attachment_links_entity", links.c.org_id, links.c.entity_type, links.c.entity_id)
    return attachments, links
