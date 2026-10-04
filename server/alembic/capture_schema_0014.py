"""Frozen Slice 13 schema; later live mapping changes cannot rewrite this migration."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB


def define_capture_tables(metadata):
    """Terminal operation records are inserted with their domain outcome, never updated."""
    streams = Table(
        "capture_streams",
        metadata,
        Column("org_id", Uuid, primary_key=True),
        Column("client_id", Uuid, primary_key=True),
        Column("client_epoch", Uuid, primary_key=True),
        Column("last_seq", BigInteger, nullable=False, server_default=text("0")),
        Column(
            "actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        CheckConstraint("last_seq>=0", name="ck_capture_streams_sequence"),
        ForeignKeyConstraint(
            ["org_id"], ["fleetops.organizations.id"], name="fk_capture_streams_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["fleetops.actors.org_id", "fleetops.actors.id"],
            name="fk_capture_streams_actor",
        ),
    )
    operations = Table(
        "capture_operations",
        metadata,
        Column("operation_id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column(
            "actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column("claimed_actor_id", Uuid, nullable=False),
        Column("client_id", Uuid, nullable=False),
        Column("client_epoch", Uuid, nullable=False),
        Column("client_seq", BigInteger, nullable=False),
        Column("entity_type", Text, nullable=False),
        Column("entity_id", Uuid, nullable=False),
        Column("expected_version", Integer),
        Column("operation", Text, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        Column("sync_state", Text, nullable=False),
        Column("result", JSONB, nullable=False),
        Column("sequence_flags", JSONB, nullable=False),
        UniqueConstraint("org_id", "operation_id", name="uq_capture_operations_org_id"),
        ForeignKeyConstraint(
            ["org_id"], ["fleetops.organizations.id"], name="fk_capture_operations_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["fleetops.actors.org_id", "fleetops.actors.id"],
            name="fk_capture_operations_actor",
        ),
        ForeignKeyConstraint(
            ["org_id", "client_id", "client_epoch"],
            ["fleetops.capture_streams." + c for c in ("org_id", "client_id", "client_epoch")],
            name="fk_capture_operations_stream",
        ),
        CheckConstraint(
            "client_seq>0 AND (expected_version IS NULL OR expected_version>0)",
            name="ck_capture_operations_version_sequence",
        ),
        CheckConstraint(
            "operation IN ('RECEIVE_SCAN','MOVE','ASSIGN','UNASSIGN',"
            "'TRANSITION','ATTACH_EVIDENCE','RESOLVE')",
            name="ck_capture_operations_kind",
        ),
        # DUPLICATE is a response classification; PENDING_GOVERNANCE is reserved only.
        CheckConstraint(
            "sync_state IN ('APPLIED','REJECTED','DUPLICATE','PENDING_GOVERNANCE')",
            name="ck_capture_operations_state_vocabulary",
        ),
        CheckConstraint(
            "sync_state IN ('APPLIED','REJECTED')", name="ck_capture_operations_terminal"
        ),
        CheckConstraint(
            "jsonb_typeof(payload)='object' AND "
            "octet_length(payload::text)<=65536 AND "
            "jsonb_typeof(result)='object' AND "
            "octet_length(result::text)<=262144 AND "
            "jsonb_typeof(sequence_flags)='array'",
            name="ck_capture_operations_json",
        ),
        CheckConstraint(
            "isfinite(occurred_at) AND isfinite(recorded_at)", name="ck_capture_operations_times"
        ),
    )
    Index(
        "ix_capture_operations_stream",
        operations.c.org_id,
        operations.c.client_id,
        operations.c.client_epoch,
        operations.c.client_seq,
    )
    conflicts = Table(
        "sync_conflicts",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("operation_id", Uuid, nullable=False),
        Column("asset_id", Uuid, nullable=False),
        Column("expected_version", Integer, nullable=False),
        Column("current_version", Integer, nullable=False),
        Column("expected_facts", JSONB, nullable=False),
        Column("current_facts", JSONB, nullable=False),
        Column(
            "actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        UniqueConstraint("org_id", "id", name="uq_sync_conflicts_org_id"),
        UniqueConstraint("operation_id", name="uq_sync_conflicts_operation"),
        ForeignKeyConstraint(
            ["org_id"], ["fleetops.organizations.id"], name="fk_sync_conflicts_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fleetops.assets.org_id", "fleetops.assets.id"],
            name="fk_sync_conflicts_asset",
        ),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["fleetops.actors.org_id", "fleetops.actors.id"],
            name="fk_sync_conflicts_actor",
        ),
        ForeignKeyConstraint(
            ["org_id", "operation_id"],
            ["fleetops.capture_operations.org_id", "fleetops.capture_operations.operation_id"],
            name="fk_sync_conflicts_operation",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint(
            "expected_version>0 AND current_version>0 AND expected_version<>current_version",
            name="ck_sync_conflicts_mismatch",
        ),
        CheckConstraint(
            "jsonb_typeof(expected_facts)='object' AND jsonb_typeof(current_facts)='object'",
            name="ck_sync_conflicts_facts",
        ),
        CheckConstraint(
            "isfinite(occurred_at) AND isfinite(recorded_at)", name="ck_sync_conflicts_times"
        ),
    )
    events = Table(
        "sync_conflict_events",
        metadata,
        Column("id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("exception_id", Uuid, nullable=False),
        Column("event_seq", Integer, nullable=False),
        Column("from_status", Text, nullable=False),
        Column("to_status", Text, nullable=False),
        Column("note", Text),
        Column(
            "actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column("occurred_at", DateTime(timezone=True), nullable=False),
        Column(
            "recorded_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        UniqueConstraint(
            "org_id", "exception_id", "event_seq", name="uq_sync_conflict_events_sequence"
        ),
        ForeignKeyConstraint(
            ["org_id"], ["fleetops.organizations.id"], name="fk_sync_conflict_events_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "exception_id"],
            ["fleetops.sync_conflicts.org_id", "fleetops.sync_conflicts.id"],
            name="fk_sync_conflict_events_exception",
        ),
        ForeignKeyConstraint(
            ["org_id", "actor_id"],
            ["fleetops.actors.org_id", "fleetops.actors.id"],
            name="fk_sync_conflict_events_actor",
        ),
        CheckConstraint(
            "event_seq>0 AND ((from_status='OPEN' AND to_status IN "
            "('ACKNOWLEDGED','RESOLVED','WAIVED')) OR "
            "(from_status='ACKNOWLEDGED' AND to_status IN ('RESOLVED','WAIVED'))) "
            "AND (note IS NULL OR (length(note) BETWEEN 1 AND 4000 AND "
            "note=fleetops.normalize_correction_reason(note) AND "
            "note ~ '[^[:space:]]')) AND "
            "(to_status='ACKNOWLEDGED' OR note IS NOT NULL)",
            name="ck_sync_conflict_events_transition",
        ),
        CheckConstraint(
            "isfinite(occurred_at) AND isfinite(recorded_at)", name="ck_sync_conflict_events_times"
        ),
    )
    return streams, operations, conflicts, events
