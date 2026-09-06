"""Organizations, typed attribution, local users, and opaque sessions.

Revision ID: 0002_identity_auth
Revises: 0001_empty_baseline

This revision owns a schema snapshot: future runtime metadata changes must not alter
what upgrading through 0002 creates. No password-bearing user is seeded here.
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

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0002_identity_auth"
down_revision = "0001_empty_baseline"
branch_labels = None
depends_on = None

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
    CheckConstraint("type IN ('HUMAN', 'SYSTEM', 'DEVICE', 'INTEGRATION')", name="ck_actors_type"),
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
        "role IN ('VENDOR', 'MANUFACTURER', 'CUSTOMER', 'CARRIER', 'SUBCONTRACTOR', 'INTERNAL')",
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


def upgrade() -> None:
    connection = op.get_bind()
    metadata.create_all(connection, checkfirst=False)
    for table in metadata.sorted_tables:
        apply_tenant_policy(
            connection,
            table,
            privileges=("SELECT",) if table.name == "organizations" else ("SELECT", "INSERT"),
        )
    # Revocation changes only the active flag; the app cannot replace a credential,
    # move it to another user, extend its lifetime, or rewrite its capture time.
    connection.exec_driver_sql("GRANT UPDATE (active) ON fleetops.sessions TO fleetops_app")
    # ADR-002's only context-free exception. Ownership bypass is intentional and is
    # constrained by exact credential matching and explicit same-tenant active checks.
    # Fully qualified tables plus the catalog-first path prevent name substitution.
    connection.exec_driver_sql("""
CREATE FUNCTION fleetops.resolve_session(token_digest bytea)
RETURNS TABLE (org_id uuid, user_id uuid, actor_id uuid)
LANGUAGE sql STABLE STRICT SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $resolver$
    SELECT s.org_id, u.id, a.id
    FROM fleetops.sessions AS s
    JOIN fleetops.users AS u ON u.org_id = s.org_id AND u.id = s.user_id
    JOIN fleetops.actors AS a ON a.org_id = u.org_id AND a.id = u.actor_id
    WHERE s.token_digest = $1
      AND pg_catalog.octet_length($1) = 32
      AND s.active AND s.expires_at > pg_catalog.statement_timestamp()
      AND u.active AND a.active AND a.type = 'HUMAN'
$resolver$
    """)
    connection.exec_driver_sql(
        "REVOKE ALL ON FUNCTION fleetops.resolve_session(bytea) FROM PUBLIC, fleetops_app"
    )
    connection.exec_driver_sql(
        "GRANT EXECUTE ON FUNCTION fleetops.resolve_session(bytea) TO fleetops_app"
    )
    # Permanent UUIDv7 bootstrap identities; no credential is a migration input.
    from uuid import UUID

    organization_id = UUID("01a0744a-17c4-7497-a69b-9da2aa8403da")
    actor_id = UUID("01a0744a-17c5-72b0-a295-fdf4f7feb13d")
    party_id = UUID("01a0744a-17c6-7996-bcbc-8d39271a6e0d")
    connection.execute(organizations.insert().values(id=organization_id, name="Defiant"))
    # The trusted migration creates the initial attribution identity. Its self-reference
    # provides the bootstrap root without a NULL creator escape for later runtime rows.
    connection.execute(
        actors.insert().values(
            id=actor_id,
            org_id=organization_id,
            type="HUMAN",
            display_name="Defiant bootstrap",
            created_by_actor_id=actor_id,
        )
    )
    connection.execute(
        parties.insert().values(
            id=party_id,
            org_id=organization_id,
            display_name="Defiant",
            created_by_actor_id=actor_id,
        )
    )
    connection.execute(
        party_roles.insert().values(
            id=UUID("01a0744a-17c7-7ebb-b7b7-d212d20fc3a1"),
            org_id=organization_id,
            party_id=party_id,
            role="INTERNAL",
        )
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION fleetops.resolve_session(bytea)")
    metadata.drop_all(op.get_bind(), checkfirst=False)
