"""Typed receiving reality and immutable reconciliation relationships (ADR-010/011/012)."""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)


def define_receiving_tables(metadata):
    """Register current receiving tables; the migration holds its own frozen copy.

    Receipt completion is an immutable witness, not a mutable fulfillment projection.
    It closes the receipt-local comparison population without editing earlier reality.
    """

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
            ForeignKeyConstraint(
                ["org_id", "actor_id"],
                ["fleetops.actors.org_id", "fleetops.actors.id"],
                name=f"fk_{name}_actor",
            ),
            CheckConstraint("isfinite(occurred_at)", name=f"ck_{name}_occurred_at"),
        )
        Index(f"ix_{name}_actor", table.c.org_id, table.c.actor_id)
        return table

    def reference(table, column, target):
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", column],
                [f"fleetops.{target}.org_id", f"fleetops.{target}.id"],
                name=f"fk_{table.name}_{column}",
            )
        )
        Index(f"ix_{table.name}_{column}", table.c.org_id, table.c[column])

    receipts = relation(
        "receipts",
        Column("vendor_party_id", Uuid, nullable=False),
        Column("vendor_role", Text, Computed("'VENDOR'::text", persisted=True), nullable=False),
        Column("po_id", Uuid),
        Column("dock_location_id", Uuid),
        Column("packing_reference", Text),
        Column("received_at", DateTime(timezone=True), nullable=False),
    )
    for field, target in (
        ("vendor_party_id", "parties"),
        ("po_id", "purchase_orders"),
        ("dock_location_id", "locations"),
    ):
        reference(receipts, field, target)
    receipts.append_constraint(
        ForeignKeyConstraint(
            ["org_id", "vendor_party_id", "vendor_role"],
            [
                "fleetops.party_roles.org_id",
                "fleetops.party_roles.party_id",
                "fleetops.party_roles.role",
            ],
            name="fk_receipts_vendor_role",
        )
    )
    receipts.append_constraint(CheckConstraint("isfinite(received_at)", name="ck_receipts_time"))
    receipts.append_constraint(
        CheckConstraint(
            "packing_reference IS NULL OR (length(btrim(packing_reference)) BETWEEN 1 AND 500)",
            name="ck_receipts_packing_reference",
        )
    )

    comparators = relation(
        "receipt_comparators",
        Column("receipt_id", Uuid, nullable=False),
        Column("po_line_id", Uuid, nullable=False),
    )
    reference(comparators, "receipt_id", "receipts")
    reference(comparators, "po_line_id", "purchase_order_lines")
    comparators.append_constraint(
        UniqueConstraint(
            "org_id",
            "receipt_id",
            "po_line_id",
            name="uq_receipt_comparators_target",
        )
    )

    lines = relation(
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
    )
    for field, target in (
        ("receipt_id", "receipts"),
        ("po_line_id", "purchase_order_lines"),
        ("item_id", "items"),
        ("owner_party_id", "parties"),
        ("custodian_party_id", "parties"),
        ("asset_id", "assets"),
    ):
        reference(lines, field, target)
    lines.append_constraint(
        ForeignKeyConstraint(
            ["org_id", "receipt_id", "po_line_id"],
            [
                "fleetops.receipt_comparators.org_id",
                "fleetops.receipt_comparators.receipt_id",
                "fleetops.receipt_comparators.po_line_id",
            ],
            name="fk_receipt_lines_comparator",
        )
    )
    lines.append_constraint(UniqueConstraint("org_id", "asset_id", name="uq_receipt_lines_asset"))
    for expression, name in (
        ("quantity > 0 AND quantity < 'Infinity'::numeric", "quantity"),
        (
            "packing_quantity IS NULL OR "
            "(packing_quantity >= 0 AND packing_quantity < 'Infinity'::numeric)",
            "packing",
        ),
        ("uom IN ('EA','M','MM','CM','IN','FT','G','MG','KG','ML','L')", "uom"),
        ("condition IN ('GOOD','DAMAGED','OPENED','UNKNOWN')", "condition"),
        ("notes IS NULL OR length(notes) <= 4000", "notes"),
        (
            "(observed_identifier_type IS NULL) = (observed_identifier_value IS NULL)",
            "observed_pair",
        ),
        (
            "observed_identifier_type IN ('MANUFACTURER_SERIAL','PCB_SERIAL','MAC','IMEI','OTHER')",
            "observed_type",
        ),
        (
            "observed_identifier_value IS NULL OR "
            "length(btrim(observed_identifier_value)) BETWEEN 1 AND 500",
            "observed_value",
        ),
        (
            "(serialized AND owner_party_id IS NOT NULL AND "
            "((asset_id IS NOT NULL AND observed_identifier_value IS NULL) OR "
            "(asset_id IS NULL AND observed_identifier_value IS NOT NULL))) OR "
            "(NOT serialized AND asset_id IS NULL AND observed_identifier_value IS NULL "
            "AND owner_party_id IS NULL AND custodian_party_id IS NULL)",
            "unit_shape",
        ),
    ):
        lines.append_constraint(CheckConstraint(expression, name=f"ck_receipt_lines_{name}"))
    Index("ix_receipt_lines_population", lines.c.org_id, lines.c.receipt_id, lines.c.po_line_id)

    reconciliations = relation(
        "receipt_reconciliations",
        Column("receipt_id", Uuid, nullable=False),
    )
    reference(reconciliations, "receipt_id", "receipts")
    reconciliations.append_constraint(
        UniqueConstraint(
            "org_id",
            "receipt_id",
            name="uq_receipt_reconciliations_receipt",
        )
    )

    exceptions = relation(
        "receiving_exceptions",
        Column("exception_type", Text, nullable=False),
        Column("receipt_id", Uuid, nullable=False),
        Column("receipt_line_id", Uuid),
        Column("po_line_id", Uuid),
        Column("asset_id", Uuid),
        Column("conflicting_asset_id", Uuid),
    )
    for field, target in (
        ("receipt_id", "receipts"),
        ("receipt_line_id", "receipt_lines"),
        ("po_line_id", "purchase_order_lines"),
        ("asset_id", "assets"),
        ("conflicting_asset_id", "assets"),
    ):
        reference(exceptions, field, target)
    exceptions.append_constraint(
        CheckConstraint(
            "exception_type IN ('SHORT','OVER','SUBSTITUTION','DAMAGED','OPENED',"
            "'SERIAL_UNREADABLE','SERIAL_MISMATCH','UNEXPECTED_ITEM',"
            "'QUANTITY_VARIANCE','UOM_MISMATCH')",
            name="ck_receiving_exceptions_type",
        )
    )
    exceptions.append_constraint(
        CheckConstraint(
            "(exception_type IN ('SHORT','OVER') AND receipt_line_id IS NULL "
            "AND po_line_id IS NOT NULL AND asset_id IS NULL AND conflicting_asset_id IS NULL) OR "
            "(exception_type IN ('SUBSTITUTION','UOM_MISMATCH') AND receipt_line_id IS NOT NULL "
            "AND po_line_id IS NOT NULL AND conflicting_asset_id IS NULL) OR "
            "(exception_type IN ('DAMAGED','OPENED') AND receipt_line_id IS NOT NULL "
            "AND conflicting_asset_id IS NULL) OR "
            "(exception_type = 'SERIAL_UNREADABLE' AND receipt_line_id IS NOT NULL "
            "AND asset_id IS NOT NULL AND conflicting_asset_id IS NULL) OR "
            "(exception_type = 'SERIAL_MISMATCH' AND receipt_line_id IS NOT NULL "
            "AND asset_id IS NULL AND conflicting_asset_id IS NOT NULL) OR "
            "(exception_type = 'UNEXPECTED_ITEM' AND receipt_line_id IS NOT NULL "
            "AND po_line_id IS NULL AND conflicting_asset_id IS NULL) OR "
            "(exception_type = 'QUANTITY_VARIANCE' AND receipt_line_id IS NOT NULL "
            "AND asset_id IS NULL AND conflicting_asset_id IS NULL)",
            name="ck_receiving_exceptions_shape",
        )
    )
    exceptions.append_constraint(
        ForeignKeyConstraint(
            ["org_id", "receipt_id", "po_line_id"],
            [
                "fleetops.receipt_comparators.org_id",
                "fleetops.receipt_comparators.receipt_id",
                "fleetops.receipt_comparators.po_line_id",
            ],
            name="fk_receiving_exceptions_comparator",
        )
    )
    Index(
        "uq_receiving_exceptions_line",
        exceptions.c.org_id,
        exceptions.c.receipt_line_id,
        exceptions.c.exception_type,
        unique=True,
        postgresql_where=text("receipt_line_id IS NOT NULL"),
    )
    Index(
        "uq_receiving_exceptions_aggregate",
        exceptions.c.org_id,
        exceptions.c.receipt_id,
        exceptions.c.po_line_id,
        exceptions.c.exception_type,
        unique=True,
        postgresql_where=text("receipt_line_id IS NULL"),
    )
    return receipts, comparators, lines, reconciliations, exceptions
