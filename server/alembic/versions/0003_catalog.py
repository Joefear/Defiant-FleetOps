"""Catalog items and non-unique external references (D1, D2, D7, D13, D18; ADR-003).

Revision ID: 0003_catalog
Revises: 0002_identity_auth

The schema is a frozen migration snapshot. Existing Slice 2 objects are FK targets
only; their definitions, grants, and authentication boundary are not changed.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0003_catalog"
down_revision = "0002_identity_auth"
branch_labels = None
depends_on = None

metadata = MetaData(schema="fleetops")
# Local FK-resolution stubs are never created or dropped by this revision.
Table("organizations", metadata, Column("id", Uuid))
Table("actors", metadata, Column("org_id", Uuid), Column("id", Uuid))
Table("parties", metadata, Column("org_id", Uuid), Column("id", Uuid))
Table(
    "party_roles", metadata, Column("org_id", Uuid), Column("party_id", Uuid), Column("role", Text)
)

items = Table(
    "items",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("manufacturer_party_id", Uuid, nullable=False),
    # D7: a seller is not automatically a manufacturer. The fixed discriminator and
    # membership FK also prevent deleting/retyping a MANUFACTURER role in use.
    Column(
        "manufacturer_role", Text, Computed("'MANUFACTURER'::text", persisted=True), nullable=False
    ),
    Column("manufacturer_part_number", Text, nullable=False),
    Column("revision", Text),
    Column("description", Text, nullable=False),
    # D13: only the catalog default. Future transactional lines capture their own UOM.
    Column("uom", Text, nullable=False),
    Column("serialized", Boolean, nullable=False),
    Column("export_classification", Text),
    Column("export_controlled", Boolean, server_default=text("false"), nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column("updated_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_items_org_id_id"),
    # ADR-003: absent revision cannot provide a loophole for duplicate catalog entries.
    # This is duplicate prevention; the permanent UUID remains the entity identity.
    UniqueConstraint(
        "org_id",
        "manufacturer_party_id",
        "manufacturer_part_number",
        "revision",
        name="uq_items_catalog_entry",
        postgresql_nulls_not_distinct=True,
    ),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_items_org"),
    ForeignKeyConstraint(
        ["org_id", "manufacturer_party_id"],
        ["fleetops.parties.org_id", "fleetops.parties.id"],
        name="fk_items_manufacturer_party",
    ),
    ForeignKeyConstraint(
        ["org_id", "manufacturer_party_id", "manufacturer_role"],
        [
            "fleetops.party_roles.org_id",
            "fleetops.party_roles.party_id",
            "fleetops.party_roles.role",
        ],
        name="fk_items_manufacturer_membership",
    ),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_items_creator",
    ),
    ForeignKeyConstraint(
        ["org_id", "updated_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_items_updater",
    ),
    CheckConstraint(
        "uom IN ('EA', 'M', 'MM', 'CM', 'IN', 'FT', 'G', 'MG', 'KG', 'ML', 'L')",
        name="ck_items_uom",
    ),
    CheckConstraint(
        "length(btrim(manufacturer_part_number)) BETWEEN 1 AND 200", name="ck_items_mpn"
    ),
    CheckConstraint(
        "revision IS NULL OR length(btrim(revision)) BETWEEN 1 AND 200", name="ck_items_revision"
    ),
    CheckConstraint("length(btrim(description)) BETWEEN 1 AND 4000", name="ck_items_description"),
    CheckConstraint(
        "export_classification IS NULL OR length(btrim(export_classification)) BETWEEN 1 AND 200",
        name="ck_items_classification",
    ),
)
external_references = Table(
    "external_references",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Uuid, nullable=False),
    Column("system", Text, nullable=False),
    Column("reference_type", Text, nullable=False),
    Column("external_value", Text, nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_external_references_org_id_id"),
    # ADR-003 permits a coarser external catalog to name multiple internal revisions.
    # Only repeating the same attachment to the same target is a duplicate.
    UniqueConstraint(
        "org_id",
        "entity_type",
        "entity_id",
        "system",
        "reference_type",
        "external_value",
        name="uq_external_references_attachment",
    ),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_external_references_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_external_references_creator",
    ),
    CheckConstraint(
        "entity_type IN ('ITEM', 'PARTY')",
        name="ck_external_references_entity_type",
    ),
    CheckConstraint(
        "length(btrim(system)) BETWEEN 1 AND 200", name="ck_external_references_system"
    ),
    CheckConstraint(
        "length(btrim(reference_type)) BETWEEN 1 AND 100", name="ck_external_references_type"
    ),
    CheckConstraint(
        "length(btrim(external_value)) BETWEEN 1 AND 500", name="ck_external_references_value"
    ),
)
Index("ix_items_creator", items.c.org_id, items.c.created_by_actor_id)
Index("ix_items_updater", items.c.org_id, items.c.updated_by_actor_id)
# The catalog uniqueness index already starts with (org_id, manufacturer_party_id).
Index(
    "ix_external_references_creator",
    external_references.c.org_id,
    external_references.c.created_by_actor_id,
)
# Searching an external value must remain non-unique, even when an index serves it.
Index(
    "ix_external_references_search",
    external_references.c.org_id,
    external_references.c.system,
    external_references.c.reference_type,
    external_references.c.external_value,
)


def upgrade() -> None:
    """Create only Slice 3 tables and use the established fail-closed RLS helper."""
    connection = op.get_bind()
    for table in (items, external_references):
        table.create(connection, checkfirst=False)
        apply_tenant_policy(connection, table)
    # UUID, org, fixed membership discriminator, and original attribution are not mutable.
    # Deactivation is UPDATE(active); physical deletion receives no runtime grant.
    connection.exec_driver_sql("""GRANT UPDATE (
    manufacturer_party_id, manufacturer_part_number, revision, description, uom,
    serialized, export_classification, export_controlled, active,
    updated_by_actor_id, updated_at
) ON fleetops.items TO fleetops_app""")


def downgrade() -> None:
    """Return to Slice 2 without touching its rows, policies, grants, or resolver."""
    connection = op.get_bind()
    external_references.drop(connection, checkfirst=False)
    items.drop(connection, checkfirst=False)
