"""Facilities and location ancestry (D10, D11, D18; ADR-001 and ADR-002).

Revision ID: 0004_space
Revises: 0003_catalog

Frozen schema snapshot: prior tables are FK targets only.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
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

revision = "0004_space"
down_revision = "0003_catalog"
branch_labels = None
depends_on = None

metadata = MetaData(schema="fleetops")
# Resolution stubs are never created or dropped by this migration.
Table("organizations", metadata, Column("id", Uuid))
Table("actors", metadata, Column("org_id", Uuid), Column("id", Uuid))

facilities = Table(
    "facilities",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("name", Text, nullable=False),
    Column("timezone", Text, nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_facilities_org_id_id"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_facilities_org"),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_facilities_creator",
    ),
    CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="ck_facilities_name"),
    CheckConstraint("length(btrim(timezone)) BETWEEN 1 AND 200", name="ck_facilities_timezone"),
)
locations = Table(
    "locations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("facility_id", Uuid, nullable=False),
    Column("parent_location_id", Uuid),
    Column("code", Text, nullable=False),
    Column("name", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_locations_org_id_id"),
    UniqueConstraint("org_id", "facility_id", "id", name="uq_locations_org_facility_id"),
    # RACK-01 may exist in two facilities; a code is local geography, not global identity.
    UniqueConstraint("org_id", "facility_id", "code", name="uq_locations_facility_code"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_locations_org"),
    ForeignKeyConstraint(
        ["org_id", "facility_id"],
        ["fleetops.facilities.org_id", "fleetops.facilities.id"],
        name="fk_locations_facility",
    ),
    # Carrying the facility through the parent key prevents cross-facility ancestry,
    # including direct SQL writes. NULL parent remains a valid root.
    ForeignKeyConstraint(
        ["org_id", "facility_id", "parent_location_id"],
        ["fleetops.locations.org_id", "fleetops.locations.facility_id", "fleetops.locations.id"],
        name="fk_locations_parent",
    ),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_locations_creator",
    ),
    CheckConstraint(
        "parent_location_id IS NULL OR parent_location_id <> id",
        name="ck_locations_not_self_parent",
    ),
    CheckConstraint(
        "kind IN ('SITE', 'ROOM', 'RACK', 'BIN', 'STATION', 'DOCK', 'VEHICLE', 'OTHER')",
        name="ck_locations_kind",
    ),
    CheckConstraint("length(btrim(code)) BETWEEN 1 AND 200", name="ck_locations_code"),
    CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="ck_locations_name"),
)
Index("ix_facilities_creator", facilities.c.org_id, facilities.c.created_by_actor_id)
Index("ix_locations_creator", locations.c.org_id, locations.c.created_by_actor_id)
Index(
    "ix_locations_parent",
    locations.c.org_id,
    locations.c.facility_id,
    locations.c.parent_location_id,
)
# The unique indexes already cover tenant/facility lookups.


def upgrade() -> None:
    """Create only space tables with fail-closed RLS and SELECT/INSERT grants."""
    connection = op.get_bind()
    for table in (facilities, locations):
        table.create(connection, checkfirst=False)
        apply_tenant_policy(connection, table)
    # There is no reassignment or deactivation workflow. UPDATE would enable parent
    # changes and require a stronger durable cycle guard; active alone is no reason
    # to grant it. DELETE and TRUNCATE are likewise outside this slice.


def downgrade() -> None:
    """Remove children before facilities, leaving Slice 1–3 data and security intact."""
    connection = op.get_bind()
    locations.drop(connection, checkfirst=False)
    facilities.drop(connection, checkfirst=False)
