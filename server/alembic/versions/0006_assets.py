"""Asset history and global concurrency (D3, D7, D8, D10-D12, D18; ADR-004/005).

Revision ID: 0006_assets
Revises: 0005_space_cycle_guard

Frozen schema and ADR-006 privilege snapshot. Prior Table objects are foreign-key
stubs; the explicit forward grants/triggers below harden their runtime attribution.
No committed migration is changed and no future history table is created.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0006_assets"
down_revision = "0005_space_cycle_guard"
branch_labels = None
depends_on = None
metadata = MetaData(schema="fleetops")
Table("organizations", metadata, Column("id", Uuid))
for name in ("actors", "parties", "locations"):
    Table(name, metadata, Column("org_id", Uuid), Column("id", Uuid))
Table("items", metadata, Column("org_id", Uuid), Column("id", Uuid), Column("serialized", Boolean))

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
        "current_state IN ("
        + (
            "'RECEIVED', 'IN_STOCK', 'CONFIGURING', 'READY', 'DEPLOYED', "
            "'ON_HOLD', 'OUT_OF_SERVICE', 'RETIRED'"
        )
        + ")",
        name="ck_assets_state",
    ),
    CheckConstraint("version > 0", name="ck_assets_version"),
    # There is no assignment authority or target table until Slice 7.
    CheckConstraint("current_assignment_id IS NULL", name="ck_assets_assignment_unavailable"),
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
        "type IN (" + "'MANUFACTURER_SERIAL', 'PCB_SERIAL', 'MAC', 'IMEI', 'OTHER'" + ")",
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
        "from_state IN ("
        + (
            "'RECEIVED', 'IN_STOCK', 'CONFIGURING', 'READY', 'DEPLOYED', "
            "'ON_HOLD', 'OUT_OF_SERVICE', 'RETIRED'"
        )
        + ")",
        name="ck_asset_transitions_from_state",
    ),
    CheckConstraint(
        "to_state IN ("
        + (
            "'RECEIVED', 'IN_STOCK', 'CONFIGURING', 'READY', 'DEPLOYED', "
            "'ON_HOLD', 'OUT_OF_SERVICE', 'RETIRED'"
        )
        + ")",
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


FUNCTION_SIGNATURE = (
    "fleetops.transition_asset(uuid, integer, text, text, text, timestamptz, uuid, uuid, uuid)"
)
TRANSITION_SQL = """
CREATE FUNCTION fleetops.transition_asset(
    p_asset_id uuid, p_expected_version integer, p_from_state text, p_to_state text,
    p_reason text, p_occurred_at timestamptz,
    p_evidence_ref uuid, p_client_op_id uuid, p_transition_id uuid
) RETURNS fleetops.asset_transitions
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $transition$
DECLARE
    trusted_org uuid;
    authenticated_actor uuid;
    locked_asset fleetops.assets%ROWTYPE;
    latest fleetops.asset_transitions%ROWTYPE;
    produced fleetops.asset_transitions%ROWTYPE;
BEGIN
    trusted_org := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    IF trusted_org IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Trusted organization required';
    END IF;
    -- D10 / ADR-006: possession of a non-enumerable, non-mintable credential is
    -- the authority. No function argument or Actor GUC can select the performer.
    authenticated_actor := fleetops.current_authenticated_actor();
    -- ADR-005: every operation competes for this same row. Compare the version only
    -- after FOR UPDATE returns; an earlier service read is not a concurrency check.
    SELECT a.* INTO locked_asset FROM fleetops.assets a
      WHERE a.org_id = trusted_org AND a.id = p_asset_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Asset not found';
    END IF;
    IF p_expected_version IS NULL OR locked_asset.version <> p_expected_version THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'Asset version is stale';
    END IF;
    SELECT t.* INTO latest FROM fleetops.asset_transitions t
      WHERE t.org_id = trusted_org AND t.asset_id = p_asset_id
      ORDER BY t.result_version DESC LIMIT 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Authoritative history missing';
    END IF;
    -- D3: a projection cannot testify on its own behalf. ADR-005 permits result-version
    -- gaps after orthogonal operations, but history ahead of the Asset is impossible.
    IF latest.result_version > locked_asset.version
       OR latest.to_state IS DISTINCT FROM p_from_state
       OR locked_asset.current_state IS DISTINCT FROM latest.to_state THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Asset history is inconsistent';
    END IF;
    IF p_evidence_ref IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Evidence is unavailable';
    END IF;
    -- UUIDv7 is generated by the trusted Python service, matching the repository's
    -- identity convention. The request API cannot choose this additional ID parameter.
    INSERT INTO fleetops.asset_transitions
        (id, org_id, asset_id, result_version, from_state, to_state, reason, actor_id,
         occurred_at, evidence_ref, client_op_id, corrects_transition_id)
    VALUES (p_transition_id, trusted_org, p_asset_id, p_expected_version + 1,
            p_from_state, p_to_state, p_reason, authenticated_actor, p_occurred_at,
            p_evidence_ref, p_client_op_id, NULL)
    RETURNING * INTO produced;
    -- D8: lifecycle changes no owner, custodian, location, or assignment projection.
    UPDATE fleetops.assets SET current_state = p_to_state, version = p_expected_version + 1
      WHERE org_id = trusted_org AND id = p_asset_id;
    RETURN produced;
END
$transition$
"""


AUTH_SQL = """
-- ADR-006 category 2: only the separate password-verifying login code may issue.
-- This function validates issuance, not Argon2; Python and the issuer credential
-- are the trusted login seam. There is deliberately no upsert or Actor argument.
CREATE FUNCTION fleetops.issue_session(
    p_user_id uuid, p_session_id uuid, p_token_digest bytea, p_expires_at timestamptz
) RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $issue$
DECLARE
    login_org uuid := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    now_at timestamptz := pg_catalog.statement_timestamp();
BEGIN
    IF login_org IS NULL OR NOT EXISTS (
        SELECT 1 FROM fleetops.users u JOIN fleetops.actors a
          ON a.org_id = u.org_id AND a.id = u.actor_id
        WHERE u.org_id = login_org AND u.id = p_user_id
          AND u.active AND a.active AND a.type = 'HUMAN'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Valid login identity required';
    END IF;
    IF p_session_id IS NULL OR p_token_digest IS NULL
       OR pg_catalog.octet_length(p_token_digest) <> 32
       OR p_expires_at IS NULL OR NOT pg_catalog.isfinite(p_expires_at)
       OR p_expires_at <= now_at OR p_expires_at > now_at + interval '24 hours' THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid session issuance';
    END IF;
    INSERT INTO fleetops.sessions(id, org_id, user_id, token_digest, created_at, expires_at)
    VALUES (p_session_id, login_org, p_user_id, p_token_digest, now_at, p_expires_at);
END
$issue$;

-- This helper needs no elevation: ADR-002's exact-digest resolver already owns
-- credential access. Re-resolve on each call, including after in-transaction revocation.
CREATE FUNCTION fleetops.current_authenticated_actor()
RETURNS uuid LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $actor$
DECLARE
    current_org uuid := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    digest_hex text := pg_catalog.current_setting('fleetops.session_digest_hex', true);
    resolved_org uuid;
    resolved_actor uuid;
BEGIN
    IF current_org IS NULL OR digest_hex IS NULL
       OR digest_hex !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Authenticated credential required';
    END IF;
    SELECT r.org_id, r.actor_id INTO resolved_org, resolved_actor
    FROM fleetops.resolve_session(pg_catalog.decode(digest_hex, 'hex')) r;
    IF NOT FOUND OR resolved_org IS DISTINCT FROM current_org OR resolved_actor IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Authenticated credential required';
    END IF;
    RETURN resolved_actor;
END
$actor$;

-- Category 2: exact-current-session revocation replaces direct runtime session DML.
CREATE FUNCTION fleetops.revoke_current_session()
RETURNS void LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $revoke$
BEGIN
    PERFORM fleetops.current_authenticated_actor();
    UPDATE fleetops.sessions SET active = false
    WHERE org_id = pg_catalog.current_setting('fleetops.org_id')::uuid
      AND token_digest = pg_catalog.decode(
          pg_catalog.current_setting('fleetops.session_digest_hex'), 'hex')
      AND active AND expires_at > pg_catalog.statement_timestamp();
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Authenticated credential required';
    END IF;
END
$revoke$;

-- AFTER INSERT preserves RLS admission and tenant-FK error semantics. A mismatch
-- still aborts the entire statement, so false creator testimony can never commit.
CREATE FUNCTION fleetops.enforce_authenticated_creator()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $creator$
DECLARE
    performer uuid;
    row_data jsonb;
BEGIN
    -- session_user remains fleetops_app even inside a SECURITY DEFINER operation.
    -- Only a deliberate independent deployment/admin login bypasses this invariant.
    IF session_user = 'fleetops_app' THEN
        performer := fleetops.current_authenticated_actor();
        row_data := pg_catalog.to_jsonb(NEW);
        IF (row_data ->> TG_ARGV[0])::uuid IS DISTINCT FROM performer
           OR (row_data ? 'updated_by_actor_id' AND
               (row_data ->> 'updated_by_actor_id')::uuid IS DISTINCT FROM performer) THEN
            RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Authenticated Actor mismatch';
        END IF;
    END IF;
    RETURN NEW;
END
$creator$;

-- BEFORE UPDATE makes attribution unavoidable even when raw SQL omits both fields.
CREATE FUNCTION fleetops.enforce_authenticated_updater()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $updater$
BEGIN
    IF session_user = 'fleetops_app' THEN
        -- A proposed tenant move must reach WITH CHECK and fail there; invisible
        -- rows never fire a row trigger. No default org or attribution is invented.
        IF NEW.org_id IS DISTINCT FROM
           NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid THEN
            RETURN NEW;
        END IF;
        NEW.updated_by_actor_id := fleetops.current_authenticated_actor();
        NEW.updated_at := pg_catalog.statement_timestamp();
    END IF;
    RETURN NEW;
END
$updater$;

-- ADR-004 initialization is an INSERT invariant, including ordinary owner INSERT.
-- It neither blocks later projection updates nor fabricates initial history.
CREATE FUNCTION fleetops.enforce_asset_initial_state()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $initial$
BEGIN
    IF NEW.version IS DISTINCT FROM 1 OR NEW.current_state IS DISTINCT FROM 'RECEIVED' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            CONSTRAINT = 'ck_assets_initial', MESSAGE = 'Asset must begin at version 1 / RECEIVED';
    END IF;
    RETURN NEW;
END
$initial$;
"""

AUTH_FUNCTIONS = {
    "issue_session(uuid, uuid, bytea, timestamptz)": "fleetops_authenticator",
    "current_authenticated_actor()": "fleetops_app",
    "revoke_current_session()": "fleetops_app",
    "enforce_authenticated_creator()": None,
    "enforce_authenticated_updater()": None,
    "enforce_asset_initial_state()": None,
}
CREATOR_TABLES = (
    "actors",
    "parties",
    "items",
    "external_references",
    "facilities",
    "locations",
    "assets",
    "asset_identifiers",
    "asset_transitions",
)


def upgrade_authentication(connection) -> None:
    """Apply ADR-006 at head without changing historical 0002's grant contract."""
    # Revoke every live column ACL as well as table ACLs, including the old logout grant.
    for name in ("users", "sessions"):
        columns = (
            connection.exec_driver_sql(
                "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass "
                "AND attnum>0 AND NOT attisdropped ORDER BY attnum",
                (f"fleetops.{name}",),
            )
            .scalars()
            .all()
        )
        connection.exec_driver_sql(f"REVOKE ALL ON fleetops.{name} FROM PUBLIC, fleetops_app")
        connection.exec_driver_sql(
            f"REVOKE ALL ({', '.join(columns)}) ON fleetops.{name} FROM PUBLIC, fleetops_app"
        )
    for name, columns in (
        ("users", "id, org_id, actor_id, username, password_hash, active"),
        ("actors", "id, org_id, type, active"),
    ):
        predicate = "org_id = NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid"
        for policy, restrictive in (
            ("authenticator_access", ""),
            ("authenticator_boundary", "AS RESTRICTIVE"),
        ):
            connection.exec_driver_sql(
                f"CREATE POLICY {policy} ON fleetops.{name} {restrictive} "
                f"TO fleetops_authenticator USING ({predicate}) WITH CHECK ({predicate})"
            )
        connection.exec_driver_sql(
            f"GRANT SELECT ({columns}) ON fleetops.{name} TO fleetops_authenticator"
        )
    connection.exec_driver_sql(AUTH_SQL)
    for signature, role in AUTH_FUNCTIONS.items():
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION fleetops.{signature} "
            "FROM PUBLIC, fleetops_app, fleetops_authenticator"
        )
        if role:
            connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION fleetops.{signature} TO {role}")
    for name in CREATOR_TABLES:
        column = "actor_id" if name == "asset_transitions" else "created_by_actor_id"
        connection.exec_driver_sql(
            f"CREATE TRIGGER zz_authenticated_creator AFTER INSERT ON fleetops.{name} "
            f"FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_creator('{column}')"
        )
    for name, columns in (
        (
            "items",
            "manufacturer_party_id, manufacturer_part_number, revision, description, "
            "uom, serialized, export_classification, export_controlled, active, "
            "updated_by_actor_id, updated_at",
        ),
        ("assets", "asset_tag, description, updated_by_actor_id, updated_at"),
    ):
        connection.exec_driver_sql(
            f"CREATE TRIGGER authenticated_updater BEFORE UPDATE OF {columns} ON fleetops.{name} "
            "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_updater()"
        )
    connection.exec_driver_sql(
        "CREATE TRIGGER asset_initial_state BEFORE INSERT ON fleetops.assets "
        "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_asset_initial_state()"
    )


def downgrade_authentication(connection) -> None:
    """Restore exact historical 0005 ACLs; the admin-owned third role remains outside Alembic."""
    for name in CREATOR_TABLES:
        connection.exec_driver_sql(f"DROP TRIGGER zz_authenticated_creator ON fleetops.{name}")
    for name in ("items", "assets"):
        connection.exec_driver_sql(f"DROP TRIGGER authenticated_updater ON fleetops.{name}")
    connection.exec_driver_sql("DROP TRIGGER asset_initial_state ON fleetops.assets")
    for signature in reversed(AUTH_FUNCTIONS):
        connection.exec_driver_sql(f"DROP FUNCTION fleetops.{signature}")
    for name in ("users", "actors"):
        columns = (
            connection.exec_driver_sql(
                "SELECT attname FROM pg_attribute WHERE attrelid=%s::regclass "
                "AND attnum>0 AND NOT attisdropped ORDER BY attnum",
                (f"fleetops.{name}",),
            )
            .scalars()
            .all()
        )
        connection.exec_driver_sql(
            f"REVOKE ALL ({', '.join(columns)}) ON fleetops.{name} FROM fleetops_authenticator"
        )
        for policy in ("authenticator_access", "authenticator_boundary"):
            connection.exec_driver_sql(f"DROP POLICY {policy} ON fleetops.{name}")
    connection.exec_driver_sql(
        "GRANT SELECT, INSERT ON fleetops.users, fleetops.sessions TO fleetops_app"
    )
    connection.exec_driver_sql("GRANT UPDATE (active) ON fleetops.sessions TO fleetops_app")


def upgrade() -> None:
    """Create Asset structures and the ADR-006 authentication/attribution foundation.

    The Asset row lock and atomic N -> N+1 write allocate global versions. No second
    version register exists. Direct history INSERT is withheld: the sole production
    write path pairs history with its projection inside this function (D3).
    """
    connection = op.get_bind()
    op.create_unique_constraint(
        "uq_items_org_id_serialized", "items", ["org_id", "id", "serialized"], schema="fleetops"
    )
    for table in (assets, asset_identifiers, asset_transitions):
        table.create(connection, checkfirst=False)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
    connection.exec_driver_sql(
        "GRANT UPDATE (asset_tag, description, updated_by_actor_id, updated_at) "
        "ON fleetops.assets TO fleetops_app"
    )
    upgrade_authentication(connection)
    connection.execute(text(TRANSITION_SQL))
    connection.exec_driver_sql(
        f"REVOKE ALL ON FUNCTION {FUNCTION_SIGNATURE} FROM PUBLIC, fleetops_app"
    )
    connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {FUNCTION_SIGNATURE} TO fleetops_app")


def downgrade() -> None:
    """Restore 0005 exactly, including removing the Item uniqueness support added here."""
    connection = op.get_bind()
    connection.exec_driver_sql(f"DROP FUNCTION {FUNCTION_SIGNATURE}")
    downgrade_authentication(connection)
    for table in (asset_transitions, asset_identifiers, assets):
        table.drop(connection, checkfirst=False)
    op.drop_constraint("uq_items_org_id_serialized", "items", type_="unique", schema="fleetops")
