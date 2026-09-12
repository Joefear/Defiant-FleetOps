"""SQLAlchemy schema for identity, authentication, catalog, space, and Asset history.

Governing decisions: D1, D2, D3, D7, D8, D10, D11, D12, D13, D18; ADR-004/005.

Tenant-safe references always carry org_id. User authentication state is separate from
typed attribution; a DEVICE actor has no need for a password-bearing user.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from fleetops.db.receiving_schema import define_receiving_tables
from fleetops.domain.actor_types import ActorType
from fleetops.domain.identifier_types import IdentifierType
from fleetops.domain.lifecycle import AssetState
from fleetops.domain.location_kinds import LocationKind
from fleetops.domain.party_roles import PartyRole
from fleetops.domain.procurement_status import PurchaseOrderStatus
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
    UniqueConstraint("org_id", "id", "serialized", name="uq_items_org_id_serialized"),
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

# Slice 5: receipt-owned creation is deliberately absent from the production service.
assets = Table(
    "assets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("item_id", Uuid, nullable=False),
    # ADR-004: the fixed TRUE key also prevents changing a referenced Item to nonserialized.
    Column("item_serialized", Boolean, Computed("true", persisted=True), nullable=False),
    Column("asset_tag", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("owner_party_id", Uuid, nullable=False),
    Column("custodian_party_id", Uuid),
    Column("current_location_id", Uuid),
    Column("current_assignment_id", Uuid),
    Column("current_state", Text, nullable=False, server_default=text("'RECEIVED'")),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column("updated_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    UniqueConstraint("org_id", "id", name="uq_assets_org_id_id"),
    UniqueConstraint("org_id", "asset_tag", name="uq_assets_tag"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_assets_org"),
    ForeignKeyConstraint(
        ["org_id", "item_id"], ["fleetops.items.org_id", "fleetops.items.id"], name="fk_assets_item"
    ),
    ForeignKeyConstraint(
        ["org_id", "item_id", "item_serialized"],
        ["fleetops.items.org_id", "fleetops.items.id", "fleetops.items.serialized"],
        name="fk_assets_serialized_item",
    ),
    ForeignKeyConstraint(
        ["org_id", "owner_party_id"],
        ["fleetops.parties.org_id", "fleetops.parties.id"],
        name="fk_assets_owner",
    ),
    ForeignKeyConstraint(
        ["org_id", "custodian_party_id"],
        ["fleetops.parties.org_id", "fleetops.parties.id"],
        name="fk_assets_custodian",
    ),
    ForeignKeyConstraint(
        ["org_id", "current_location_id"],
        ["fleetops.locations.org_id", "fleetops.locations.id"],
        name="fk_assets_location",
    ),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_assets_creator",
    ),
    ForeignKeyConstraint(
        ["org_id", "updated_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_assets_updater",
    ),
    CheckConstraint(
        "current_state IN (" + ", ".join(repr(s.value) for s in AssetState) + ")",
        name="ck_assets_state",
    ),
    CheckConstraint("version > 0", name="ck_assets_version"),
    CheckConstraint("length(btrim(asset_tag)) BETWEEN 1 AND 200", name="ck_assets_tag"),
    CheckConstraint("length(btrim(description)) BETWEEN 1 AND 4000", name="ck_assets_description"),
)

asset_identifiers = Table(
    "asset_identifiers",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("asset_id", Uuid, nullable=False),
    Column("type", Text, nullable=False),
    Column("value", Text),
    Column("unreadable_reason", Text),
    Column("created_by_actor_id", Uuid, nullable=False),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    ),
    UniqueConstraint("org_id", "id", name="uq_asset_identifiers_org_id_id"),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_identifiers_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_identifiers_asset",
    ),
    ForeignKeyConstraint(
        ["org_id", "created_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_asset_identifiers_creator",
    ),
    CheckConstraint(
        "type IN (" + ", ".join(repr(t.value) for t in IdentifierType) + ")",
        name="ck_asset_identifiers_type",
    ),
    # An unreadable label is missing evidence, not the world's most common serial number.
    CheckConstraint(
        "(value IS NOT NULL AND unreadable_reason IS NULL "
        "AND length(btrim(value)) BETWEEN 1 AND 500) "
        "OR (value IS NULL AND unreadable_reason IS NOT NULL "
        "AND length(unreadable_reason) <= 4000 AND unreadable_reason ~ '[^[:space:]]')",
        name="ck_asset_identifiers_readability",
    ),
)
Index(
    "uq_asset_identifiers_readable",
    asset_identifiers.c.org_id,
    asset_identifiers.c.type,
    asset_identifiers.c.value,
    unique=True,
    postgresql_where=asset_identifiers.c.value.is_not(None),
)

asset_transitions = Table(
    "asset_transitions",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("asset_id", Uuid, nullable=False),
    # ADR-005: global produced version; gaps within state history are legitimate.
    Column("result_version", Integer, nullable=False),
    Column("from_state", Text),
    Column("to_state", Text, nullable=False),
    Column("reason", Text, nullable=False),
    Column("actor_id", Uuid, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    ),
    Column("evidence_ref", Uuid),
    Column("corrects_transition_id", Uuid),
    Column("client_op_id", Uuid),
    UniqueConstraint("org_id", "id", name="uq_asset_transitions_org_id_id"),
    UniqueConstraint("org_id", "asset_id", "id", name="uq_asset_transitions_asset_id"),
    UniqueConstraint("org_id", "asset_id", "result_version", name="uq_asset_transitions_version"),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_transitions_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_transitions_asset",
    ),
    ForeignKeyConstraint(
        ["org_id", "actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_asset_transitions_actor",
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id", "corrects_transition_id"],
        [
            "fleetops.asset_transitions.org_id",
            "fleetops.asset_transitions.asset_id",
            "fleetops.asset_transitions.id",
        ],
        name="fk_asset_transitions_corrects",
    ),
    CheckConstraint("result_version > 0", name="ck_asset_transitions_version"),
    CheckConstraint(
        "from_state IN (" + ", ".join(repr(s.value) for s in AssetState) + ")",
        name="ck_asset_transitions_from_state",
    ),
    CheckConstraint(
        "to_state IN (" + ", ".join(repr(s.value) for s in AssetState) + ")",
        name="ck_asset_transitions_to_state",
    ),
    CheckConstraint(
        "(result_version = 1 AND from_state IS NULL AND to_state = 'RECEIVED') "
        "OR (result_version > 1 AND from_state IS NOT NULL)",
        name="ck_asset_transitions_initial",
    ),
    CheckConstraint("length(btrim(reason)) BETWEEN 1 AND 4000", name="ck_asset_transitions_reason"),
    # ADR-004: the interface exists now; evidence cannot be verified until Slice 11.
    CheckConstraint("evidence_ref IS NULL", name="ck_asset_transitions_evidence_unavailable"),
)
for table, columns in (
    (
        assets,
        (
            "item_id",
            "owner_party_id",
            "custodian_party_id",
            "current_location_id",
            "created_by_actor_id",
            "updated_by_actor_id",
        ),
    ),
    (asset_identifiers, ("asset_id", "created_by_actor_id")),
    (asset_transitions, ("actor_id",)),
):
    for column in columns:
        Index(f"ix_{table.name}_{column}", table.c.org_id, table.c[column])

# ADR-007: creation truth is immutable and contributes no additional global version.
# Asset identity is also baseline identity; absence stays visible rather than backfilled.
asset_initial_facts = Table(
    "asset_initial_facts",
    metadata,
    Column("asset_id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("initial_owner_party_id", Uuid, nullable=False),
    Column("initial_custodian_party_id", Uuid),
    Column("initial_location_id", Uuid),
    Column("actor_id", Uuid, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    ),
    UniqueConstraint("org_id", "asset_id", name="uq_asset_initial_facts_asset"),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_initial_facts_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_initial_facts_asset",
    ),
)
for field, target in (
    ("initial_owner_party_id", "parties"),
    ("initial_custodian_party_id", "parties"),
    ("initial_location_id", "locations"),
    ("actor_id", "actors"),
):
    asset_initial_facts.append_constraint(
        ForeignKeyConstraint(
            ["org_id", field],
            [f"fleetops.{target}.org_id", f"fleetops.{target}.id"],
            name=f"fk_asset_initial_facts_{field}",
        )
    )
    Index(
        f"ix_asset_initial_facts_{field}",
        asset_initial_facts.c.org_id,
        asset_initial_facts.c[field],
    )


def _physical_history(name, from_field, to_field, target, correction, *, nullable):
    """Define independent temporal facts with tenant/Asset-safe correction seams.

    The per-class unique version supports authoritative ordering; the shared Asset
    lock, not a second register, serializes versions across these separate tables.
    """
    table = Table(
        name,
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("asset_id", Uuid, nullable=False),
        Column("result_version", Integer, nullable=False),
        Column(from_field, Uuid, nullable=nullable),
        Column(to_field, Uuid, nullable=nullable),
        Column("actor_id", Uuid, nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        Column("reason", Text, nullable=False),
        Column(correction, Uuid),
        Column("client_op_id", Uuid),
        UniqueConstraint("org_id", "id", name=f"uq_{name}_org_id_id"),
        UniqueConstraint("org_id", "asset_id", "id", name=f"uq_{name}_asset_id"),
        UniqueConstraint("org_id", "asset_id", "result_version", name=f"uq_{name}_version"),
        ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name=f"fk_{name}_org"),
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fleetops.assets.org_id", "fleetops.assets.id"],
            name=f"fk_{name}_asset",
        ),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["fleetops.actors.org_id", "fleetops.actors.id"],
            name=f"fk_{name}_actor",
        ),
        # D6: normal operations leave this inert. A later correction cannot cross Assets.
        ForeignKeyConstraint(
            ["org_id", "asset_id", correction],
            [f"fleetops.{name}.org_id", f"fleetops.{name}.asset_id", f"fleetops.{name}.id"],
            name=f"fk_{name}_corrects",
        ),
        CheckConstraint("result_version > 0", name=f"ck_{name}_version"),
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 1 AND 4000 AND reason ~ '[^[:space:]]'",
            name=f"ck_{name}_reason",
        ),
    )
    for field in (from_field, to_field):
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", field],
                [f"fleetops.{target}.org_id", f"fleetops.{target}.id"],
                name=f"fk_{name}_{field}",
            )
        )
        Index(f"ix_{name}_{field}", table.c.org_id, table.c[field])
    Index(f"ix_{name}_actor", table.c.org_id, table.c.actor_id)
    return table


# Nullable location records loss of represented location without inventing a destination.
asset_movements = _physical_history(
    "asset_movements",
    "from_location_id",
    "to_location_id",
    "locations",
    "corrects_movement_id",
    nullable=True,
)
asset_custody_changes = _physical_history(
    "asset_custody_changes",
    "from_custodian_party_id",
    "to_custodian_party_id",
    "parties",
    "corrects_custody_change_id",
    nullable=True,
)
asset_ownership_changes = _physical_history(
    "asset_ownership_changes",
    "from_owner_party_id",
    "to_owner_party_id",
    "parties",
    "corrects_ownership_change_id",
    nullable=False,
)

# ADR-008: row existence positively attests initially unassigned, not an unknown assignee.
asset_initial_assignment_facts = Table(
    "asset_initial_assignment_facts",
    metadata,
    Column("asset_id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("actor_id", Uuid, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    ),
    UniqueConstraint("org_id", "asset_id", name="uq_asset_initial_assignment_facts_asset"),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_initial_assignment_facts_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_initial_assignment_facts_asset",
    ),
    ForeignKeyConstraint(
        ["org_id", "actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_asset_initial_assignment_facts_actor",
    ),
)
Index(
    "ix_asset_initial_assignment_facts_actor",
    asset_initial_assignment_facts.c.org_id,
    asset_initial_assignment_facts.c.actor_id,
)

asset_assignment_events = Table(
    "asset_assignment_events",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("asset_id", Uuid, nullable=False),
    Column("result_version", Integer, nullable=False),
    Column("from_assignee_type", Text),
    Column("from_assignee_id", Uuid),
    Column("to_assignee_type", Text),
    Column("to_assignee_id", Uuid),
    Column("actor_id", Uuid, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    ),
    Column("reason", Text, nullable=False),
    Column("corrects_assignment_event_id", Uuid),
    Column("client_op_id", Uuid),
    UniqueConstraint("org_id", "id", name="uq_asset_assignment_events_org_id_id"),
    UniqueConstraint("org_id", "asset_id", "id", name="uq_asset_assignment_events_asset_id"),
    UniqueConstraint(
        "org_id", "asset_id", "result_version", name="uq_asset_assignment_events_version"
    ),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_assignment_events_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_assignment_events_asset",
    ),
    ForeignKeyConstraint(
        ["org_id", "actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_asset_assignment_events_actor",
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id", "corrects_assignment_event_id"],
        [
            "fleetops.asset_assignment_events.org_id",
            "fleetops.asset_assignment_events.asset_id",
            "fleetops.asset_assignment_events.id",
        ],
        name="fk_asset_assignment_events_corrects",
    ),
    CheckConstraint("result_version > 0", name="ck_asset_assignment_events_version"),
    CheckConstraint(
        "length(btrim(reason)) BETWEEN 1 AND 4000 AND reason ~ '[^[:space:]]'",
        name="ck_asset_assignment_events_reason",
    ),
    CheckConstraint(
        "from_assignee_id IS NOT NULL OR to_assignee_id IS NOT NULL",
        name="ck_asset_assignment_events_change",
    ),
)
for endpoint in ("from", "to"):
    asset_assignment_events.append_constraint(
        CheckConstraint(
            f"({endpoint}_assignee_type IS NULL) = ({endpoint}_assignee_id IS NULL)",
            name=f"ck_asset_assignment_events_{endpoint}_pair",
        )
    )
    asset_assignment_events.append_constraint(
        CheckConstraint(
            f"{endpoint}_assignee_type IN ('ACTOR', 'LOCATION', 'PARTY')",
            name=f"ck_asset_assignment_events_{endpoint}_type",
        )
    )
Index(
    "ix_asset_assignment_events_actor",
    asset_assignment_events.c.org_id,
    asset_assignment_events.c.actor_id,
)

# Identity allocates configuration order only. The API deliberately omits this global value.
asset_configurations = Table(
    "asset_configurations",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("asset_id", Uuid, nullable=False),
    Column("image_name", Text, nullable=False),
    Column("image_version", Text, nullable=False),
    Column("config_profile", Text, nullable=False),
    Column("notes", Text, nullable=False),
    Column(
        "applied_by",
        Uuid,
        nullable=False,
        server_default=text("fleetops.current_authenticated_actor()"),
    ),
    Column("applied_at", DateTime(timezone=True), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=text("statement_timestamp()"),
    ),
    Column("evidence_ref", Uuid),
    Column("configuration_seq", BigInteger, Identity(always=True, cache=1), nullable=False),
    UniqueConstraint("org_id", "id", name="uq_asset_configurations_org_id_id"),
    UniqueConstraint("configuration_seq", name="uq_asset_configurations_seq"),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_asset_configurations_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "asset_id"],
        ["fleetops.assets.org_id", "fleetops.assets.id"],
        name="fk_asset_configurations_asset",
    ),
    ForeignKeyConstraint(
        ["org_id", "applied_by"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_asset_configurations_actor",
    ),
    CheckConstraint("evidence_ref IS NULL", name="ck_asset_configurations_evidence_unavailable"),
    CheckConstraint("configuration_seq > 0", name="ck_asset_configurations_seq"),
    CheckConstraint("length(notes) <= 4000", name="ck_asset_configurations_notes"),
)
for field in ("image_name", "image_version", "config_profile"):
    asset_configurations.append_constraint(
        CheckConstraint(
            f"length(btrim({field})) BETWEEN 1 AND 200 AND {field} ~ '[^[:space:]]'",
            name=f"ck_asset_configurations_{field}",
        )
    )
Index(
    "ix_asset_configurations_history",
    asset_configurations.c.org_id,
    asset_configurations.c.asset_id,
    asset_configurations.c.configuration_seq,
)
Index(
    "ix_asset_configurations_actor",
    asset_configurations.c.org_id,
    asset_configurations.c.applied_by,
)

# The back-reference is installed after event creation; it must name this Asset's event.
assets.append_constraint(
    ForeignKeyConstraint(
        ["org_id", "id", "current_assignment_id"],
        [
            "fleetops.asset_assignment_events.org_id",
            "fleetops.asset_assignment_events.asset_id",
            "fleetops.asset_assignment_events.id",
        ],
        name="fk_assets_assignment",
        use_alter=True,
    )
)

# ADR-009: historical versions share a business line number, never record identity.
purchase_orders = Table(
    "purchase_orders",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("vendor_party_id", Uuid, nullable=False),
    Column("vendor_role", Text, Computed("'VENDOR'::text", persisted=True), nullable=False),
    Column("po_number", Text, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'DRAFT'")),
    Column("issued_at", DateTime(timezone=True)),
    Column("issued_by_actor_id", Uuid),
    Column("notes", Text),
    UniqueConstraint("org_id", "id", name="uq_purchase_orders_org_id_id"),
    ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name="fk_purchase_orders_org"),
    ForeignKeyConstraint(
        ["org_id", "vendor_party_id"],
        ["fleetops.parties.org_id", "fleetops.parties.id"],
        name="fk_purchase_orders_vendor",
    ),
    ForeignKeyConstraint(
        ["org_id", "vendor_party_id", "vendor_role"],
        [
            "fleetops.party_roles.org_id",
            "fleetops.party_roles.party_id",
            "fleetops.party_roles.role",
        ],
        name="fk_purchase_orders_vendor_role",
    ),
    ForeignKeyConstraint(
        ["org_id", "issued_by_actor_id"],
        ["fleetops.actors.org_id", "fleetops.actors.id"],
        name="fk_purchase_orders_issuer",
    ),
    CheckConstraint(
        "status IN (" + ", ".join(repr(v.value) for v in PurchaseOrderStatus) + ")",
        name="ck_purchase_orders_status",
    ),
    CheckConstraint(
        "(issued_at IS NULL) = (issued_by_actor_id IS NULL) "
        "AND (status <> 'DRAFT' OR issued_at IS NULL) "
        "AND (status <> 'ISSUED' OR issued_at IS NOT NULL)",
        name="ck_purchase_orders_issuance",
    ),
    CheckConstraint(
        "issued_at IS NULL OR isfinite(issued_at)", name="ck_purchase_orders_issued_time"
    ),
    CheckConstraint(
        "length(btrim(po_number)) BETWEEN 1 AND 200 AND po_number ~ '[^[:space:]]'",
        name="ck_purchase_orders_number",
    ),
    CheckConstraint("notes IS NULL OR length(notes) <= 4000", name="ck_purchase_orders_notes"),
)
purchase_order_lines = Table(
    "purchase_order_lines",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("org_id", Uuid, nullable=False),
    Column("po_id", Uuid, nullable=False),
    Column("line_number", Integer, nullable=False),
    Column("item_id", Uuid, nullable=False),
    # Unconstrained NUMERIC preserves decimal input without implicit scale rounding.
    Column("quantity", Numeric, nullable=False),
    Column("uom", Text, nullable=False),
    Column("unit_price", Numeric, nullable=False),
    Column("expected_date", Date),
    Column("supersedes_line_id", Uuid),
    UniqueConstraint("org_id", "id", name="uq_purchase_order_lines_org_id_id"),
    UniqueConstraint(
        "org_id", "po_id", "line_number", "id", name="uq_purchase_order_lines_lineage_id"
    ),
    ForeignKeyConstraint(
        ["org_id"], ["fleetops.organizations.id"], name="fk_purchase_order_lines_org"
    ),
    ForeignKeyConstraint(
        ["org_id", "po_id"],
        ["fleetops.purchase_orders.org_id", "fleetops.purchase_orders.id"],
        name="fk_purchase_order_lines_po",
    ),
    ForeignKeyConstraint(
        ["org_id", "item_id"],
        ["fleetops.items.org_id", "fleetops.items.id"],
        name="fk_purchase_order_lines_item",
    ),
    ForeignKeyConstraint(
        ["org_id", "po_id", "line_number", "supersedes_line_id"],
        [
            "fleetops.purchase_order_lines.org_id",
            "fleetops.purchase_order_lines.po_id",
            "fleetops.purchase_order_lines.line_number",
            "fleetops.purchase_order_lines.id",
        ],
        name="fk_purchase_order_lines_predecessor",
    ),
    CheckConstraint("line_number > 0", name="ck_purchase_order_lines_number"),
    CheckConstraint(
        "quantity > 0 AND quantity < 'Infinity'::numeric", name="ck_purchase_order_lines_quantity"
    ),
    CheckConstraint(
        "unit_price >= 0 AND unit_price < 'Infinity'::numeric", name="ck_purchase_order_lines_price"
    ),
    CheckConstraint(
        "expected_date IS NULL OR isfinite(expected_date)",
        name="ck_purchase_order_lines_expected_date",
    ),
    CheckConstraint("id <> supersedes_line_id", name="ck_purchase_order_lines_not_self"),
    CheckConstraint(
        "uom IN (" + ", ".join(repr(v.value) for v in UnitOfMeasure) + ")",
        name="ck_purchase_order_lines_uom",
    ),
)
for procurement_table in (purchase_orders, purchase_order_lines):
    for attribution in ("created", "updated"):
        procurement_table.append_column(Column(f"{attribution}_by_actor_id", Uuid, nullable=False))
        procurement_table.append_column(
            Column(
                f"{attribution}_at",
                DateTime(timezone=True),
                nullable=False,
                server_default=text("statement_timestamp()"),
            )
        )
        procurement_table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", f"{attribution}_by_actor_id"],
                ["fleetops.actors.org_id", "fleetops.actors.id"],
                name=f"fk_{procurement_table.name}_{attribution}_actor",
            )
        )
        Index(
            f"ix_{procurement_table.name}_{attribution}_actor",
            procurement_table.c.org_id,
            procurement_table.c[f"{attribution}_by_actor_id"],
        )
Index(
    "ix_purchase_orders_vendor",
    purchase_orders.c.org_id,
    purchase_orders.c.vendor_party_id,
    purchase_orders.c.vendor_role,
)
Index("ix_purchase_orders_issuer", purchase_orders.c.org_id, purchase_orders.c.issued_by_actor_id)
Index("ix_purchase_order_lines_item", purchase_order_lines.c.org_id, purchase_order_lines.c.item_id)
# One root per logical line and one successor per predecessor jointly exclude duplicate leaves.
Index(
    "uq_purchase_order_lines_root",
    purchase_order_lines.c.org_id,
    purchase_order_lines.c.po_id,
    purchase_order_lines.c.line_number,
    unique=True,
    postgresql_where=text("supersedes_line_id IS NULL"),
)
Index(
    "uq_purchase_order_lines_successor",
    purchase_order_lines.c.org_id,
    purchase_order_lines.c.supersedes_line_id,
    unique=True,
    postgresql_where=text("supersedes_line_id IS NOT NULL"),
)

(receipts, receipt_comparators, receipt_lines, receipt_reconciliations, receiving_exceptions) = (
    define_receiving_tables(metadata)
)
