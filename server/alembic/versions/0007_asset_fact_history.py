"""Immutable creation facts and atomic physical changes (ADR-005/006/007).

Revision ID: 0007_asset_fact_history
Revises: 0006_assets

This frozen schema snapshot is independent of future runtime metadata. No baseline
is synthesized from existing projections: missing creation truth stays incomplete.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
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

revision = "0007_asset_fact_history"
down_revision = "0006_assets"
branch_labels = None
depends_on = None
metadata = MetaData(schema="fleetops")
Table("organizations", metadata, Column("id", Uuid))
for name in ("assets", "actors", "parties", "locations"):
    Table(name, metadata, Column("org_id", Uuid), Column("id", Uuid))

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

# All identifiers below are fixed migration constants, never runtime SQL inputs.
OPERATIONS = (
    (
        "move_asset",
        "asset_movements",
        "current_location_id",
        "from_location_id",
        "to_location_id",
        "initial_location_id",
        "locations",
        False,
    ),
    (
        "change_custody",
        "asset_custody_changes",
        "custodian_party_id",
        "from_custodian_party_id",
        "to_custodian_party_id",
        "initial_custodian_party_id",
        "parties",
        False,
    ),
    (
        "change_ownership",
        "asset_ownership_changes",
        "owner_party_id",
        "from_owner_party_id",
        "to_owner_party_id",
        "initial_owner_party_id",
        "parties",
        True,
    ),
)
TABLES = (asset_initial_facts, asset_movements, asset_custody_changes, asset_ownership_changes)


def _signature(name):
    """The three boundaries have no tenant, performer, prior-fact or correction selector."""
    return f"fleetops.{name}(uuid, integer, uuid, text, timestamptz, uuid, uuid)"


def _operation_sql(name, history, projection, from_field, to_field, initial, target, required):
    """Expand the same Slice 5 lock protocol into three narrowly scoped static functions.

    Each function touches one history class and one physical projection. This template
    shares mechanics, not runtime authority: no caller can choose a table or column.
    """
    required_check = ""
    if required:
        required_check = f"""
    IF p_{to_field} IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23502', MESSAGE = 'Owner is required';
    END IF;
"""
    return f"""
CREATE FUNCTION fleetops.{name}(
    p_asset_id uuid, p_expected_version integer, p_{to_field} uuid,
    p_reason text, p_occurred_at timestamptz, p_client_op_id uuid, p_history_id uuid
) RETURNS fleetops.{history}
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $physical$
DECLARE
    trusted_org uuid;
    authenticated_actor uuid;
    locked_asset fleetops.assets%ROWTYPE;
    latest fleetops.{history}%ROWTYPE;
    prior_fact uuid;
    produced fleetops.{history}%ROWTYPE;
BEGIN
    trusted_org := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    IF trusted_org IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Trusted organization required';
    END IF;
    -- ADR-006: re-resolve the credential; no Actor argument or Actor GUC is authority.
    authenticated_actor := fleetops.current_authenticated_actor();
    -- Four history classes, one version number, one Asset lock. This is the same
    -- SELECT FOR UPDATE and post-lock comparison used by transition_asset.
    SELECT a.* INTO locked_asset FROM fleetops.assets a
      WHERE a.org_id = trusted_org AND a.id = p_asset_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Asset not found';
    END IF;
    IF p_expected_version IS NULL OR locked_asset.version <> p_expected_version THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'Asset version is stale';
    END IF;
    SELECT h.* INTO latest FROM fleetops.{history} h
      WHERE h.org_id = trusted_org AND h.asset_id = p_asset_id
      ORDER BY h.result_version DESC LIMIT 1;
    IF NOT FOUND THEN
        -- ADR-007: the first change needs a witness older than the projection.
        -- A present NULL fact is valid; a missing baseline is incomplete history.
        SELECT b.{initial} INTO prior_fact FROM fleetops.asset_initial_facts b
          WHERE b.org_id = trusted_org AND b.asset_id = p_asset_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Asset initial facts missing';
        END IF;
    ELSE
        IF latest.result_version > locked_asset.version THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Asset history is inconsistent';
        END IF;
        prior_fact := latest.{to_field};
    END IF;
    IF locked_asset.{projection} IS DISTINCT FROM prior_fact THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001',
            MESSAGE = 'Asset physical projection is inconsistent';
    END IF;
    {required_check}
    -- SECURITY DEFINER never relies on owner RLS to validate the target.
    IF p_{to_field} IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fleetops.{target} t
        WHERE t.org_id = trusted_org AND t.id = p_{to_field}
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Invalid physical target';
    END IF;
    -- Correction fields remain NULL. client_op_id is correlation only, not idempotency.
    INSERT INTO fleetops.{history}
        (id, org_id, asset_id, result_version, {from_field}, {to_field},
         actor_id, occurred_at, reason, client_op_id)
    VALUES (p_history_id, trusted_org, p_asset_id, p_expected_version + 1,
            prior_fact, p_{to_field}, authenticated_actor, p_occurred_at, p_reason, p_client_op_id)
    RETURNING * INTO produced;
    -- D7/D8: custody, ownership, location and lifecycle are independent facts.
    UPDATE fleetops.assets SET {projection} = p_{to_field}, version = p_expected_version + 1
      WHERE org_id = trusted_org AND id = p_asset_id;
    RETURN produced;
END
$physical$
"""


def upgrade() -> None:
    """Install only new objects; no projection backfill or production creation surface."""
    connection = op.get_bind()
    for table in TABLES:
        table.create(connection, checkfirst=False)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        # Preserve ADR-006 even under isolated test grants or a future creation boundary.
        # The existing invoker trigger checks session_user inside definer calls too.
        connection.exec_driver_sql(
            f"CREATE TRIGGER attributed_creator AFTER INSERT ON fleetops.{table.name} "
            "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_creator('actor_id')"
        )
    for operation in OPERATIONS:
        connection.execute(text(_operation_sql(*operation)))
        signature = _signature(operation[0])
        connection.exec_driver_sql(f"ALTER FUNCTION {signature} OWNER TO fleetops_migrator")
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC, fleetops_app, fleetops_authenticator"
        )
        connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {signature} TO fleetops_app")


def downgrade() -> None:
    """Remove this revision's functions/tables and their dependent triggers, policies and ACLs."""
    connection = op.get_bind()
    for operation in reversed(OPERATIONS):
        connection.exec_driver_sql(f"DROP FUNCTION {_signature(operation[0])}")
    for table in reversed(TABLES):
        table.drop(connection, checkfirst=False)
