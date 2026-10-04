"""Typed record correction, evaluation provenance, and Exception workflow mappings."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)


def define_record_corrections(metadata):
    """Keep business roots typed and every consequence pinned to immutable sources."""

    def reference(table, columns, target, target_columns=None):
        target_columns = target_columns or columns
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", *columns],
                [f"fleetops.{target}.{c}" for c in ["org_id", *target_columns]],
                name=f"fk_{table.name}_{columns[0] if len(columns) == 1 else 'source'}",
            )
        )

    def relation(name, *columns):
        table = Table(
            name,
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("org_id", Uuid, nullable=False),
            *columns,
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
            UniqueConstraint("org_id", "id", name=f"uq_{name}_org_id"),
            ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name=f"fk_{name}_org"),
            CheckConstraint("isfinite(occurred_at)", name=f"ck_{name}_occurred_at"),
        )
        reference(table, ["actor_id"], "actors", ["id"])
        return table

    def correction(name, root, parent, *columns):
        table = relation(
            name,
            Column(root, Uuid, nullable=False),
            Column(
                "recording_transaction_id",
                Text,
                nullable=False,
                server_default=text("pg_current_xact_id()::text"),
            ),
            Column("correction_role", Text, nullable=False),
            Column("correction_pair_id", Uuid, nullable=False),
            Column("correction_generation", Integer, nullable=False),
            Column("correction_occurred_at", DateTime(timezone=True), nullable=False),
            Column("reason", Text, nullable=False),
            *columns,
        )
        reference(table, [root], parent, ["id"])
        for columns_, suffix in (
            (["org_id", "correction_pair_id", "correction_role"], "pair_role"),
            (["org_id", root, "correction_generation", "correction_role"], "root_generation_role"),
            (["org_id", root, "correction_generation", "id", "correction_role"], "source"),
        ):
            table.append_constraint(UniqueConstraint(*columns_, name=f"uq_{name}_{suffix}"))
        table.append_constraint(
            CheckConstraint(
                "correction_role IN ('REVERSAL','CORRECTED') AND correction_generation>0 "
                "AND isfinite(correction_occurred_at) AND length(reason) BETWEEN 1 AND 4000 "
                "AND reason ~ '[^[:space:]]' "
                "AND reason=fleetops.normalize_correction_reason(reason)",
                name=f"ck_{name}_metadata",
            )
        )
        return table

    procurement = correction(
        "purchase_order_line_corrections",
        "po_line_id",
        "purchase_order_lines",
        Column("po_id", Uuid, nullable=False),
        Column("item_id", Uuid, nullable=False),
        Column("quantity", Numeric, nullable=False),
        Column("uom", Text, nullable=False),
        Column("unit_price", Numeric, nullable=False),
        Column("expected_date", Date),
    )
    reference(procurement, ["po_id"], "purchase_orders", ["id"])
    reference(procurement, ["item_id"], "items", ["id"])
    receipt = correction(
        "receipt_line_corrections",
        "receipt_line_id",
        "receipt_lines",
        Column("receipt_id", Uuid, nullable=False),
        Column("po_line_id", Uuid),
        Column("item_id", Uuid, nullable=False),
        Column("quantity", Numeric, nullable=False),
        Column("uom", Text, nullable=False),
        Column("condition", Text, nullable=False),
        Column("packing_quantity", Numeric),
        Column("notes", Text),
        Column("serialized", Boolean, nullable=False),
        Column("owner_party_id", Uuid),
        Column("custodian_party_id", Uuid),
        Column("asset_id", Uuid),
        Column("observed_identifier_type", Text),
        Column("observed_identifier_value", Text),
        Column("conflicting_asset_id", Uuid),
    )
    receipt.append_constraint(
        UniqueConstraint(
            "org_id",
            "receipt_id",
            "id",
            "correction_role",
            name="uq_receipt_line_corrections_introduction",
        )
    )
    for column, target in (
        ("receipt_id", "receipts"),
        ("po_line_id", "purchase_order_lines"),
        ("item_id", "items"),
        ("owner_party_id", "parties"),
        ("custodian_party_id", "parties"),
        ("asset_id", "assets"),
        ("conflicting_asset_id", "assets"),
    ):
        reference(receipt, [column], target, ["id"])
    for table in (procurement, receipt):
        table.append_constraint(
            CheckConstraint(
                "quantity>0 AND quantity<'Infinity'::numeric "
                "AND uom IN ('EA','M','MM','CM','IN','FT','G','MG','KG','ML','L')",
                name=f"ck_{table.name}_business",
            )
        )
    procurement.append_constraint(
        CheckConstraint(
            "(unit_price IS NULL OR (unit_price>=0 AND unit_price<'Infinity'::numeric)) "
            "AND (expected_date IS NULL OR isfinite(expected_date))",
            name="ck_purchase_order_line_corrections_optional",
        )
    )
    receipt.append_constraint(
        CheckConstraint(
            "condition IN ('GOOD','DAMAGED','OPENED','UNKNOWN') "
            "AND (packing_quantity IS NULL OR (packing_quantity>=0 AND "
            "packing_quantity<'Infinity'::numeric)) "
            "AND (notes IS NULL OR length(notes)<=4000) "
            "AND (observed_identifier_type IS NULL)=(observed_identifier_value IS NULL) "
            "AND (observed_identifier_value IS NULL OR "
            "length(btrim(observed_identifier_value)) BETWEEN 1 AND 500)",
            name="ck_receipt_line_corrections_observations",
        )
    )

    evaluations = relation(
        "receipt_correction_evaluations",
        Column("receipt_id", Uuid, nullable=False),
        Column("receipt_line_id", Uuid, nullable=False),
        Column("correction_id", Uuid, nullable=False),
        Column("correction_generation", Integer, nullable=False),
        Column("source_role", Text, Computed("'CORRECTED'::text", persisted=True), nullable=False),
        Column("evaluation_seq", Integer, nullable=False),
        Column(
            "recording_transaction_id",
            Text,
            nullable=False,
            server_default=text("pg_current_xact_id()::text"),
        ),
    )
    reference(evaluations, ["receipt_id"], "receipts", ["id"])
    reference(
        evaluations,
        ["receipt_line_id", "correction_generation", "correction_id", "source_role"],
        "receipt_line_corrections",
        ["receipt_line_id", "correction_generation", "id", "correction_role"],
    )
    evaluations.append_constraint(
        UniqueConstraint("org_id", "correction_id", name="uq_receipt_evaluation_correction")
    )
    evaluations.append_constraint(
        UniqueConstraint("org_id", "receipt_id", "evaluation_seq", name="uq_receipt_evaluation_seq")
    )
    evaluations.append_constraint(
        CheckConstraint("evaluation_seq>0", name="ck_receipt_evaluation_seq")
    )

    sources = []
    for name, root, target in (
        ("receipt_evaluation_lines", "receipt_line_id", "receipt_line_corrections"),
        ("receipt_evaluation_expectations", "po_line_id", "purchase_order_line_corrections"),
    ):
        table = relation(
            name,
            Column("evaluation_id", Uuid, nullable=False),
            Column(root, Uuid, nullable=False),
            Column("source_generation", Integer, nullable=False),
            Column("source_id", Uuid),
            Column(
                "source_role", Text, Computed("'CORRECTED'::text", persisted=True), nullable=False
            ),
        )
        reference(table, ["evaluation_id"], "receipt_correction_evaluations", ["id"])
        reference(
            table,
            [root],
            "receipt_lines" if root == "receipt_line_id" else "purchase_order_lines",
            ["id"],
        )
        reference(
            table,
            [root, "source_generation", "source_id", "source_role"],
            target,
            [root, "correction_generation", "id", "correction_role"],
        )
        table.append_constraint(
            UniqueConstraint("org_id", "evaluation_id", root, name=f"uq_{name}_root")
        )
        table.append_constraint(
            CheckConstraint(
                "(source_generation=0 AND source_id IS NULL) OR "
                "(source_generation>0 AND source_id IS NOT NULL)",
                name=f"ck_{name}_source",
            )
        )
        sources.append(table)

    workflows = Table(
        "exception_workflows",
        metadata,
        Column("exception_id", Uuid, primary_key=True),
        Column("org_id", Uuid, nullable=False),
        Column("exception_type", Text, nullable=False),
        Column("severity", Text, nullable=False, server_default=text("'UNSPECIFIED'")),
        Column("entity_type", Text, nullable=False),
        Column("entity_id", Uuid, nullable=False),
        Column("status", Text, nullable=False, server_default=text("'OPEN'")),
        Column("event_seq", Integer, nullable=False, server_default=text("0")),
        Column("resolved_by_actor_id", Uuid),
        Column("resolved_at", DateTime(timezone=True)),
        Column("resolution_note", Text),
        # Frozen vocabulary: future runtime enum changes must not change migration 0011.
        CheckConstraint(
            "exception_type IN ('SHORT','OVER','SUBSTITUTION','DAMAGED','OPENED',"
            "'SERIAL_UNREADABLE','SERIAL_MISMATCH','UNEXPECTED_ITEM','QUANTITY_VARIANCE',"
            "'UOM_MISMATCH','GENERAL','SYNC_CONFLICT','MISSING_LOT','BALANCE_NEGATIVE')",
            name="ck_exception_workflows_type",
        ),
        CheckConstraint("severity IN ('UNSPECIFIED')", name="ck_exception_workflows_severity"),
        CheckConstraint(
            "entity_type IN ('RECEIPT','RECEIPT_LINE')",
            name="ck_exception_workflows_entity_type",
        ),
        UniqueConstraint("org_id", "exception_id", name="uq_exception_workflows_org_id"),
        ForeignKeyConstraint(
            ["org_id"], ["fleetops.organizations.id"], name="fk_exception_workflows_org"
        ),
        CheckConstraint(
            "status IN ('OPEN','ACKNOWLEDGED','RESOLVED','WAIVED') AND event_seq>=0",
            name="ck_exception_workflows_status",
        ),
    )
    reference(workflows, ["exception_id"], "receiving_exceptions", ["id"])
    reference(workflows, ["resolved_by_actor_id"], "actors", ["id"])
    events = relation(
        "exception_events",
        Column("exception_id", Uuid, nullable=False),
        Column("event_seq", Integer, nullable=False),
        Column("from_status", Text, nullable=False),
        Column("to_status", Text, nullable=False),
        Column("note", Text),
        Column("evaluation_id", Uuid),
    )
    reference(events, ["exception_id"], "receiving_exceptions", ["id"])
    reference(events, ["evaluation_id"], "receipt_correction_evaluations", ["id"])
    events.append_constraint(
        UniqueConstraint("org_id", "exception_id", "event_seq", name="uq_exception_events_seq")
    )
    events.append_constraint(
        CheckConstraint(
            "event_seq>0 AND ((from_status='OPEN' AND to_status IN "
            "('ACKNOWLEDGED','RESOLVED','WAIVED')) "
            "OR (from_status='ACKNOWLEDGED' AND to_status IN ('RESOLVED','WAIVED'))) "
            "AND (to_status='ACKNOWLEDGED' OR (note IS NOT NULL AND "
            "length(note) BETWEEN 1 AND 4000 "
            "AND note ~ '[^[:space:]]'))"
            " AND (note IS NULL OR (length(note) BETWEEN 1 AND 4000 "
            "AND note=fleetops.normalize_correction_reason(note) "
            "AND note ~ '[^[:space:]]'))",
            name="ck_exception_events_transition",
        )
    )
    consequences = relation(
        "receipt_evaluation_exceptions",
        Column("evaluation_id", Uuid, nullable=False),
        Column("exception_id", Uuid, nullable=False),
        Column("supported", Boolean, nullable=False),
        Column("prior_event_seq", Integer, nullable=False),
    )
    reference(consequences, ["evaluation_id"], "receipt_correction_evaluations", ["id"])
    reference(consequences, ["exception_id"], "receiving_exceptions", ["id"])
    consequences.append_constraint(
        UniqueConstraint(
            "org_id", "evaluation_id", "exception_id", name="uq_receipt_evaluation_exception"
        )
    )
    for table in (procurement, receipt, evaluations, *sources, workflows, events, consequences):
        Index(f"ix_{table.name}_org", table.c.org_id)
    return (procurement, receipt, evaluations, *sources, workflows, events, consequences)
