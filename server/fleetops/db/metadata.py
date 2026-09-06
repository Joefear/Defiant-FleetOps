"""SQLAlchemy schema for identity, authentication, catalog, and space.

Governing decisions: D1, D2, D7, D10, D11, D13, D18.

Tenant-safe references always carry org_id. User authentication state is separate from
typed attribution; a DEVICE actor has no need for a password-bearing user.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from fleetops.domain.actor_types import ActorType
from fleetops.domain.location_kinds import LocationKind
from fleetops.domain.party_roles import PartyRole
from fleetops.domain.reference_types import ReferenceEntityType
from fleetops.domain.uom import UnitOfMeasure

metadata = MetaData(schema="fleetops")

organizations = Table(
    "organizations",
    metadata,
    Column("id", Uuid, primary_key=True),
    # The tenant root scopes itself. A generated org_id keeps the same fail-closed
    # policy shape on every table without permitting an independently selected scope.
    Column("org_id", Uuid, Computed("id", persisted=True), nullable=False),
    Column("name", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_organizations_org_id_id"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_organizations_self"),
    CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="ck_organizations_name"),
)
actors = Table(
    "actors",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("type", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_actors_org_id_id"),
    UniqueConstraint("org_id", "id", "type", name="uq_actors_org_id_id_type"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_actors_org"),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_actors_creator",
    ),
    CheckConstraint(
        "type IN (" + ", ".join(repr(value.value) for value in ActorType) + ")",
        name="ck_actors_type",
    ),
    CheckConstraint("length(btrim(display_name)) BETWEEN 1 AND 200", name="ck_actors_display_name"),
)
parties = Table(
    "parties",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_parties_org_id_id"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_parties_org"),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_parties_creator",
    ),
    CheckConstraint(
        "length(btrim(display_name)) BETWEEN 1 AND 200", name="ck_parties_display_name"
    ),
)
party_roles = Table(
    "party_roles",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("party_id", Uuid, nullable=False),
    Column("role", Text, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_party_roles_org_id_id"),
    UniqueConstraint("org_id", "party_id", "role", name="uq_party_roles_membership"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_party_roles_org"),
    ForeignKeyConstraint(
        ["org_id", "party_id"],
        ["fleetops.parties.org_id", "fleetops.parties.id"],
        name="fk_party_roles_party",
    ),
    CheckConstraint(
        "role IN (" + ", ".join(repr(value.value) for value in PartyRole) + ")",
        name="ck_party_roles_role",
    ),
)
users = Table(
    "users",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("actor_id", Uuid, nullable=False),
    # The pair FK enforces tenant safety; the additional typed FK makes HUMAN linkage
    # a durable constraint, including attempted type changes on an existing actor.
    Column("actor_type", Text, Computed("'HUMAN'::text", persisted=True), nullable=False),
    Column("username", Text, nullable=False),
    Column("password_hash", Text, nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    UniqueConstraint("org_id", "id", name="uq_users_org_id_id"),
    UniqueConstraint("org_id", "username", name="uq_users_org_username"),
    UniqueConstraint("org_id", "actor_id", name="uq_users_org_actor"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_users_org"),
    ForeignKeyConstraint(
        ["org_id", "actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_users_actor",
    ),
    ForeignKeyConstraint(
        ["org_id", "actor_id", "actor_type"],
        ["fleetops.actors.org_id", "fleetops.actors.id", "fleetops.actors.type"],
        name="fk_users_human_actor",
    ),
    CheckConstraint(
        "length(username) BETWEEN 1 AND 128 AND username = btrim(username)",
        name="ck_users_username",
    ),
    CheckConstraint("password_hash LIKE '$argon2id$%'", name="ck_users_argon2id"),
)
sessions = Table(
    "sessions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("user_id", Uuid, nullable=False),
    Column("token_digest", LargeBinary, nullable=False),
    Column("active", Boolean, server_default=text("true"), nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        server_default=text("CURRENT_TIMESTAMP"),
        nullable=False,
    ),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("org_id", "id", name="uq_sessions_org_id_id"),
    UniqueConstraint("token_digest", name="uq_sessions_token_digest"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_sessions_org"),
    ForeignKeyConstraint(
        ["org_id", "user_id"],
        ["fleetops.users.org_id", "fleetops.users.id"],
        name="fk_sessions_user",
    ),
    CheckConstraint("octet_length(token_digest) = 32", name="ck_sessions_sha256"),
    CheckConstraint("expires_at > created_at", name="ck_sessions_expiration"),
)
Index("ix_actors_creator", actors.c.org_id, actors.c.created_by_actor_id)
Index("ix_parties_creator", parties.c.org_id, parties.c.created_by_actor_id)
Index("ix_sessions_user", sessions.c.org_id, sessions.c.user_id)

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
        "uom IN (" + ", ".join(repr(value.value) for value in UnitOfMeasure) + ")",
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
        "entity_type IN (" + ", ".join(repr(value.value) for value in ReferenceEntityType) + ")",
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
        "kind IN (" + ", ".join(repr(value.value) for value in LocationKind) + ")",
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
