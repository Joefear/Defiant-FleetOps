"""Immutable assignment authority and independently ordered configurations (ADR-008).

Revision ID: 0008_assignment_configuration
Revises: 0007_asset_fact_history

This frozen snapshot activates only the assignment projection. It never backfills
creation testimony from present values and never makes configuration an Asset version.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Identity,
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

revision = "0008_assignment_configuration"
down_revision = "0007_asset_fact_history"
branch_labels = None
depends_on = None
metadata = MetaData(schema="fleetops")
Table("organizations", metadata, Column("id", Uuid))
for name in ("assets", "actors"):
    Table(name, metadata, Column("org_id", Uuid), Column("id", Uuid))

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


TABLES = (asset_initial_assignment_facts, asset_assignment_events, asset_configurations)
CONFIG_INSERT_COLUMNS = (
    "id",
    "org_id",
    "asset_id",
    "image_name",
    "image_version",
    "config_profile",
    "notes",
    "applied_at",
    "evidence_ref",
)


def signature(name):
    """Two fixed interfaces; no performer, tenant, prior fact or correction authority."""
    target = ", text, uuid" if name == "assign_asset" else ""
    return f"fleetops.{name}(uuid, integer{target}, text, timestamptz, uuid, uuid)"


def assignment_sql(name):
    """Emit independent, narrowly scoped Category-1 boundaries sharing the same protocol.

    The template is migration-time code, not a runtime table/operation selector.
    Reassignment follows the same path and appends once; it never calls unassign.
    """
    assigning = name == "assign_asset"
    params = "p_assignee_type text, p_assignee_id uuid," if assigning else ""
    target_type = "p_assignee_type" if assigning else "NULL::text"
    target_id = "p_assignee_id" if assigning else "NULL::uuid"
    target_check = (
        """
    IF p_assignee_type IS NULL OR p_assignee_id IS NULL
       OR p_assignee_type NOT IN ('ACTOR', 'LOCATION', 'PARTY') THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Invalid assignment target';
    END IF;
"""
        if assigning
        else """
    IF prior_id IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Asset is already unassigned';
    END IF;
"""
    )
    return f"""
CREATE FUNCTION fleetops.{name}(
    p_asset_id uuid, p_expected_version integer, {params}
    p_reason text, p_occurred_at timestamptz, p_client_op_id uuid, p_event_id uuid
) RETURNS fleetops.asset_assignment_events
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $assignment$
DECLARE
    trusted_org uuid;
    authenticated_actor uuid;
    locked_asset fleetops.assets%ROWTYPE;
    latest fleetops.asset_assignment_events%ROWTYPE;
    produced fleetops.asset_assignment_events%ROWTYPE;
    prior_type text;
    prior_id uuid;
    expected_assignment uuid;
    endpoint record;
BEGIN
    trusted_org := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    IF trusted_org IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'Trusted organization required';
    END IF;
    authenticated_actor := fleetops.current_authenticated_actor();
    -- ADR-005: five history classes compete on this exact Asset row, before version admission.
    SELECT a.* INTO locked_asset FROM fleetops.assets a
      WHERE a.org_id = trusted_org AND a.id = p_asset_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'Asset not found';
    END IF;
    IF p_expected_version IS NULL OR locked_asset.version <> p_expected_version THEN
        RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'Asset version is stale';
    END IF;
    SELECT h.* INTO latest FROM fleetops.asset_assignment_events h
      WHERE h.org_id = trusted_org AND h.asset_id = p_asset_id
      ORDER BY h.result_version DESC LIMIT 1;
    IF NOT FOUND THEN
        -- A NULL projection is a claim. The immutable creation witness supplies its evidence.
        PERFORM 1 FROM fleetops.asset_initial_assignment_facts b
          WHERE b.org_id = trusted_org AND b.asset_id = p_asset_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Initial assignment facts missing';
        END IF;
    ELSE
        IF latest.result_version > locked_asset.version THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Assignment history is inconsistent';
        END IF;
        prior_type := latest.to_assignee_type;
        prior_id := latest.to_assignee_id;
        IF prior_id IS NOT NULL THEN
            expected_assignment := latest.id;
        END IF;
    END IF;
    IF locked_asset.current_assignment_id IS DISTINCT FROM expected_assignment THEN
        RAISE EXCEPTION USING ERRCODE = 'P0001', MESSAGE = 'Assignment projection is inconsistent';
    END IF;
    {target_check}
    -- Polymorphic references cannot share one FK. Validate both represented endpoints
    -- explicitly, including a privilegedly damaged prior target; owner RLS is no substitute.
    FOR endpoint IN
        SELECT prior_type AS kind, prior_id AS id
        UNION ALL SELECT {target_type}, {target_id}
    LOOP
        IF endpoint.id IS NOT NULL AND NOT (
            (endpoint.kind = 'ACTOR' AND EXISTS (
                SELECT 1 FROM fleetops.actors t
                WHERE t.org_id = trusted_org AND t.id = endpoint.id))
            OR (endpoint.kind = 'LOCATION' AND EXISTS (
                SELECT 1 FROM fleetops.locations t
                WHERE t.org_id = trusted_org AND t.id = endpoint.id))
            OR (endpoint.kind = 'PARTY' AND EXISTS (
                SELECT 1 FROM fleetops.parties t
                WHERE t.org_id = trusted_org AND t.id = endpoint.id))
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Invalid assignment target';
        END IF;
    END LOOP;
    INSERT INTO fleetops.asset_assignment_events
        (id, org_id, asset_id, result_version, from_assignee_type, from_assignee_id,
         to_assignee_type, to_assignee_id, actor_id, occurred_at, reason, client_op_id)
    VALUES (p_event_id, trusted_org, p_asset_id, p_expected_version + 1, prior_type, prior_id,
            {target_type}, {target_id}, authenticated_actor,
            p_occurred_at, p_reason, p_client_op_id)
    RETURNING * INTO produced;
    -- D8: lifecycle and physical projections do not participate in assignment changes.
    UPDATE fleetops.assets
      SET current_assignment_id = CASE WHEN produced.to_assignee_id IS NULL THEN NULL
                                      ELSE produced.id END,
          version = p_expected_version + 1
      WHERE org_id = trusted_org AND id = p_asset_id;
    RETURN produced;
END
$assignment$
"""


def upgrade() -> None:
    """Install only Slice 7 objects and minimal ordinary INSERT privileges for configuration."""
    connection = op.get_bind()
    for table in TABLES:
        table.create(connection, checkfirst=False)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        actor = "applied_by" if table is asset_configurations else "actor_id"
        connection.exec_driver_sql(
            f"CREATE TRIGGER attributed_creator AFTER INSERT ON fleetops.{table.name} "
            f"FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_creator('{actor}')"
        )
    # Column grants protect defaulted attribution/time/ordering, including raw runtime SQL.
    # Identity generation needs no ordinary sequence privileges; no nextval/setval side door.
    connection.exec_driver_sql(
        "GRANT INSERT ("
        + ", ".join(CONFIG_INSERT_COLUMNS)
        + ") ON fleetops.asset_configurations TO fleetops_app"
    )
    connection.exec_driver_sql(
        "REVOKE ALL ON SEQUENCE fleetops.asset_configurations_configuration_seq_seq "
        "FROM PUBLIC, fleetops_app, fleetops_authenticator"
    )
    connection.exec_driver_sql(
        "ALTER TABLE fleetops.assets DROP CONSTRAINT ck_assets_assignment_unavailable"
    )
    connection.exec_driver_sql(
        "ALTER TABLE fleetops.assets ADD CONSTRAINT fk_assets_assignment "
        "FOREIGN KEY (org_id, id, current_assignment_id) "
        "REFERENCES fleetops.asset_assignment_events (org_id, asset_id, id)"
    )
    for name in ("assign_asset", "unassign_asset"):
        connection.execute(text(assignment_sql(name)))
        call = signature(name)
        connection.exec_driver_sql(f"ALTER FUNCTION {call} OWNER TO fleetops_migrator")
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION {call} FROM PUBLIC, fleetops_app, fleetops_authenticator"
        )
        connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {call} TO fleetops_app")


def downgrade() -> None:
    """Restore the NULL-only boundary without fabricating unassignment to permit downgrade."""
    connection = op.get_bind()
    # Validate before dropping anything. Active assignments make this downgrade fail atomically.
    connection.exec_driver_sql(
        "ALTER TABLE fleetops.assets ADD CONSTRAINT ck_assets_assignment_unavailable "
        "CHECK (current_assignment_id IS NULL)"
    )
    connection.exec_driver_sql("ALTER TABLE fleetops.assets DROP CONSTRAINT fk_assets_assignment")
    for name in ("unassign_asset", "assign_asset"):
        connection.exec_driver_sql(f"DROP FUNCTION {signature(name)}")
    for table in reversed(TABLES):
        table.drop(connection, checkfirst=False)
