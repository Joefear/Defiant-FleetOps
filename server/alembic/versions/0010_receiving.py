"""Receiving truth, normal creation, and typed Exceptions (ADR-010/011/012).

Revision ID: 0010_receiving
Revises: 0009_procurement

Schema and SQL are frozen in this revision. Earlier objects and privileges stay intact.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    ForeignKeyConstraint,
    Index,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy


def define_receiving_tables(metadata):
    """Define this revision's frozen receiving tables independently of runtime metadata.

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


revision = "0010_receiving"
down_revision = "0009_procurement"
branch_labels = None
depends_on = None
metadata = MetaData(schema="fleetops")
Table("organizations", metadata, Column("id", Uuid))
for name in (
    "actors",
    "parties",
    "locations",
    "items",
    "assets",
    "purchase_orders",
    "purchase_order_lines",
):
    Table(name, metadata, Column("org_id", Uuid), Column("id", Uuid))
Table(
    "party_roles", metadata, Column("org_id", Uuid), Column("party_id", Uuid), Column("role", Text)
)
TABLES = define_receiving_tables(metadata)
UNIT_SIGNATURE = (
    "fleetops.create_received_unit(uuid, uuid, uuid, uuid, numeric, text, text, numeric, "
    "text, uuid, text, text, uuid, uuid, uuid, text, text, text, uuid)"
)
GUARDS = """
-- Invoker-only locks use existing RLS. PO precedes receipt on every write path.
-- READ COMMITTED is required: an old REPEATABLE READ snapshot cannot establish
-- fresh active-leaf authority by waiting on an unchanged immutable PO row.
CREATE FUNCTION fleetops.lock_receiving_context(p_org uuid, p_receipt uuid)
RETURNS fleetops.receipts LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $context$
DECLARE
    receipt fleetops.receipts%ROWTYPE;
BEGIN
    -- Caller-supplied Actor hints are not receiving authority, even for a real Actor.
    IF NULLIF(pg_catalog.current_setting('fleetops.actor_id', true), '') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Caller Actor context is not permitted';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='40001', MESSAGE='Receiving requires READ COMMITTED';
    END IF;
    SELECT r.* INTO receipt FROM fleetops.receipts r
      WHERE r.org_id=p_org AND r.id=p_receipt;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Receipt not found';
    END IF;
    IF receipt.po_id IS NOT NULL THEN
        PERFORM 1 FROM fleetops.purchase_orders p
          WHERE p.org_id=p_org AND p.id=receipt.po_id FOR NO KEY UPDATE;
    END IF;
    SELECT r.* INTO receipt FROM fleetops.receipts r
      WHERE r.org_id=p_org AND r.id=p_receipt FOR NO KEY UPDATE;
    RETURN receipt;
END
$context$;

CREATE FUNCTION fleetops.enforce_receiving_record()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $record$
DECLARE
    receipt fleetops.receipts%ROWTYPE;
    comparator fleetops.purchase_order_lines%ROWTYPE;
    po fleetops.purchase_orders%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Receiving history is immutable';
    END IF;
    -- Caller-supplied Actor hints are not receiving authority, even for a real Actor.
    IF NULLIF(pg_catalog.current_setting('fleetops.actor_id', true), '') IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Caller Actor context is not permitted';
    END IF;
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE='40001', MESSAGE='Receiving requires READ COMMITTED';
    END IF;
    NEW.recorded_at := pg_catalog.statement_timestamp();
    IF TG_TABLE_NAME = 'receipts' THEN
        NEW.occurred_at := NEW.received_at;
        IF NEW.po_id IS NOT NULL THEN
            SELECT p.* INTO po FROM fleetops.purchase_orders p
              WHERE p.org_id=NEW.org_id AND p.id=NEW.po_id FOR NO KEY UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Purchase order not found';
            END IF;
            IF po.status <> 'ISSUED' THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Receipt requires issued PO';
            END IF;
        END IF;
        RETURN NEW;
    END IF;
    receipt := fleetops.lock_receiving_context(NEW.org_id, NEW.receipt_id);
    NEW.occurred_at := receipt.received_at;
    IF TG_TABLE_NAME IN ('receipt_lines','receipt_comparators','receipt_reconciliations')
       AND EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations f
                   WHERE f.org_id=NEW.org_id AND f.receipt_id=NEW.receipt_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Receipt population is already reconciled';
    END IF;
    IF TG_TABLE_NAME IN ('receipt_lines','receipt_comparators') THEN
      IF NEW.po_line_id IS NOT NULL THEN
        SELECT p.* INTO comparator FROM fleetops.purchase_order_lines p
          WHERE p.org_id=NEW.org_id AND p.id=NEW.po_line_id AND p.po_id=receipt.po_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Invalid receipt comparator';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM fleetops.purchase_orders p
                       WHERE p.org_id=NEW.org_id AND p.id=receipt.po_id AND p.status='ISSUED') THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Comparator requires issued PO';
        END IF;
        IF EXISTS (SELECT 1 FROM fleetops.purchase_order_lines successor
                   WHERE successor.org_id=NEW.org_id
                     AND successor.supersedes_line_id=comparator.id) THEN
            RAISE EXCEPTION USING ERRCODE='40001', MESSAGE='Receipt comparator is stale';
        END IF;
      END IF;
    END IF;
    IF TG_TABLE_NAME = 'receipt_lines' THEN
        -- This is immutable capture-time serialization evidence. Catalog policy
        -- changes cannot retroactively invent an Asset for a nonserialized arrival.
        SELECT i.serialized INTO NEW.serialized FROM fleetops.items i
          WHERE i.org_id=NEW.org_id AND i.id=NEW.item_id FOR SHARE;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Received Item not found';
        END IF;
        IF NEW.packing_quantity IS NOT NULL AND receipt.packing_reference IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Packing count requires its reference';
        END IF;
        -- ADR-012 Decision 20: each serialized line is one physical unit,
        -- including known conflicts; the captured UOM never permits a unit batch.
        IF NEW.serialized AND NEW.quantity <> 1 THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Serialized line represents one unit';
        END IF;
        IF NEW.observed_identifier_value IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM fleetops.asset_identifiers i
            WHERE i.org_id=NEW.org_id AND i.type=NEW.observed_identifier_type
              AND i.value=NEW.observed_identifier_value
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Known canonical conflict required';
        END IF;
        IF NEW.asset_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM fleetops.assets a JOIN fleetops.asset_initial_facts b
              ON b.org_id=a.org_id AND b.asset_id=a.id
            WHERE a.org_id=NEW.org_id AND a.id=NEW.asset_id AND a.item_id=NEW.item_id
              AND b.initial_owner_party_id=NEW.owner_party_id
              AND b.initial_custodian_party_id IS NOT DISTINCT FROM NEW.custodian_party_id
              AND b.initial_location_id=receipt.dock_location_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Invalid received Asset relationship';
        END IF;
    END IF;
    RETURN NEW;
END
$record$;

-- Classification generation stays in Python. This invoker validates that persisted
-- line classifications agree with their immutable facts, including raw SQL writes.
CREATE FUNCTION fleetops.enforce_receiving_exception()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $exception$
DECLARE
    line fleetops.receipt_lines%ROWTYPE;
    comparator fleetops.purchase_order_lines%ROWTYPE;
    total numeric;
    mismatch boolean;
    valid boolean := false;
BEGIN
    IF NEW.receipt_line_id IS NULL THEN
        IF NEW.exception_type NOT IN ('SHORT','OVER') THEN RETURN NEW; END IF;
        IF NOT EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations f
                       WHERE f.org_id=NEW.org_id AND f.receipt_id=NEW.receipt_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Quantity requires completed capture';
        END IF;
        SELECT p.* INTO comparator FROM fleetops.purchase_order_lines p
          JOIN fleetops.receipt_comparators c ON c.org_id=p.org_id AND c.po_line_id=p.id
          WHERE c.org_id=NEW.org_id AND c.receipt_id=NEW.receipt_id AND p.id=NEW.po_line_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Invalid aggregate comparator';
        END IF;
        SELECT coalesce(sum(l.quantity) FILTER (WHERE l.uom=comparator.uom), 0),
               coalesce(bool_or(l.uom<>comparator.uom), false)
          INTO total, mismatch FROM fleetops.receipt_lines l
          WHERE l.org_id=NEW.org_id AND l.receipt_id=NEW.receipt_id
            AND l.po_line_id=NEW.po_line_id;
        valid := NOT mismatch AND
          ((NEW.exception_type='SHORT' AND total<comparator.quantity) OR
           (NEW.exception_type='OVER' AND total>comparator.quantity));
    ELSE
        SELECT l.* INTO line FROM fleetops.receipt_lines l
          WHERE l.org_id=NEW.org_id AND l.id=NEW.receipt_line_id
            AND l.receipt_id=NEW.receipt_id;
        IF NOT FOUND OR line.po_line_id IS DISTINCT FROM NEW.po_line_id
           OR (NEW.asset_id IS NOT NULL AND NEW.asset_id IS DISTINCT FROM line.asset_id) THEN
            RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Exception relationships disagree';
        END IF;
        IF line.po_line_id IS NOT NULL THEN
            SELECT p.* INTO comparator FROM fleetops.purchase_order_lines p
              WHERE p.org_id=line.org_id AND p.id=line.po_line_id;
        END IF;
        CASE NEW.exception_type
        WHEN 'SUBSTITUTION' THEN valid := line.item_id <> comparator.item_id;
        WHEN 'DAMAGED' THEN valid := line.condition='DAMAGED';
        WHEN 'OPENED' THEN valid := line.condition='OPENED';
        WHEN 'UNEXPECTED_ITEM' THEN valid := line.po_line_id IS NULL;
        WHEN 'UOM_MISMATCH' THEN valid := line.uom<>comparator.uom;
        WHEN 'QUANTITY_VARIANCE' THEN
            valid := line.packing_quantity IS NOT NULL AND line.quantity<>line.packing_quantity;
        WHEN 'SERIAL_UNREADABLE' THEN
            valid := line.asset_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM fleetops.asset_identifiers i
                WHERE i.org_id=line.org_id AND i.asset_id=line.asset_id
                  AND i.value IS NULL AND i.unreadable_reason IS NOT NULL);
        WHEN 'SERIAL_MISMATCH' THEN
            valid := line.serialized AND line.asset_id IS NULL
              AND line.observed_identifier_type IS NOT NULL AND EXISTS (
                SELECT 1 FROM fleetops.asset_identifiers i
                WHERE i.org_id=line.org_id AND i.asset_id=NEW.conflicting_asset_id
                  AND i.type=line.observed_identifier_type
                  AND i.value=line.observed_identifier_value);
        ELSE valid := false;
        END CASE;
    END IF;
    IF valid IS DISTINCT FROM true THEN
        RAISE EXCEPTION USING ERRCODE='23514',
            MESSAGE='Exception is not supported by receipt facts';
    END IF;
    RETURN NEW;
END
$exception$;

-- Deferred presence checks prevent ordinary SQL from committing a receipt line
-- without its applicable unit exceptions. They verify, never generate, classifications.
CREATE FUNCTION fleetops.enforce_receiving_completeness()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $complete$
DECLARE
    line fleetops.receipt_lines%ROWTYPE;
    comparator fleetops.purchase_order_lines%ROWTYPE;
    kind text;
    expected text[];
    total numeric;
    mismatch boolean;
BEGIN
    IF TG_TABLE_NAME='receipt_lines' THEN
        SELECT l.* INTO line FROM fleetops.receipt_lines l
          WHERE l.org_id=NEW.org_id AND l.id=NEW.id;
        IF NOT FOUND THEN RETURN NULL; END IF;
        expected := ARRAY[]::text[];
        IF line.po_line_id IS NULL THEN
            expected := pg_catalog.array_append(expected, 'UNEXPECTED_ITEM');
        ELSE
            SELECT p.* INTO comparator FROM fleetops.purchase_order_lines p
              WHERE p.org_id=line.org_id AND p.id=line.po_line_id;
            IF line.item_id<>comparator.item_id THEN
                expected := pg_catalog.array_append(expected, 'SUBSTITUTION');
            END IF;
            IF line.uom<>comparator.uom THEN
                expected := pg_catalog.array_append(expected, 'UOM_MISMATCH');
            END IF;
        END IF;
        IF line.condition IN ('DAMAGED','OPENED') THEN
            expected := pg_catalog.array_append(expected, line.condition);
        END IF;
        IF line.packing_quantity IS NOT NULL AND line.quantity<>line.packing_quantity THEN
            expected := pg_catalog.array_append(expected, 'QUANTITY_VARIANCE');
        END IF;
        IF line.observed_identifier_value IS NOT NULL THEN
            expected := pg_catalog.array_append(expected, 'SERIAL_MISMATCH');
        END IF;
        IF line.asset_id IS NOT NULL AND EXISTS (
            SELECT 1 FROM fleetops.asset_identifiers i
            WHERE i.org_id=line.org_id AND i.asset_id=line.asset_id AND i.value IS NULL) THEN
            expected := pg_catalog.array_append(expected, 'SERIAL_UNREADABLE');
        END IF;
        FOREACH kind IN ARRAY expected LOOP
            IF NOT EXISTS (SELECT 1 FROM fleetops.receiving_exceptions e
                           WHERE e.org_id=line.org_id AND e.receipt_line_id=line.id
                             AND e.exception_type=kind) THEN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Required line Exception missing';
            END IF;
        END LOOP;
    ELSE
        FOR comparator IN
            SELECT p.* FROM fleetops.purchase_order_lines p
              JOIN fleetops.receipt_comparators c ON c.org_id=p.org_id AND c.po_line_id=p.id
              WHERE c.org_id=NEW.org_id AND c.receipt_id=NEW.receipt_id
        LOOP
            SELECT coalesce(sum(l.quantity) FILTER (WHERE l.uom=comparator.uom), 0),
                   coalesce(bool_or(l.uom<>comparator.uom), false)
              INTO total, mismatch FROM fleetops.receipt_lines l
              WHERE l.org_id=NEW.org_id AND l.receipt_id=NEW.receipt_id
                AND l.po_line_id=comparator.id;
            kind := CASE WHEN mismatch OR total=comparator.quantity THEN NULL
                         WHEN total<comparator.quantity THEN 'SHORT' ELSE 'OVER' END;
            IF kind IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM fleetops.receiving_exceptions e
                WHERE e.org_id=NEW.org_id AND e.receipt_id=NEW.receipt_id
                  AND e.po_line_id=comparator.id AND e.receipt_line_id IS NULL
                  AND e.exception_type=kind
            ) THEN
                RAISE EXCEPTION USING ERRCODE='23514',
                    MESSAGE='Required quantity Exception missing';
            END IF;
        END LOOP;
    END IF;
    RETURN NULL;
END
$complete$;
"""
CREATE_UNIT = """
-- ADR-001 category 1 / ADR-010: one receipt-owned creation operation pairs the
-- initial projection with authoritative history and both independent witnesses.
-- It has no organization/Actor argument and cannot create an unlinked manual Asset.
CREATE FUNCTION fleetops.create_received_unit(
    p_receipt_id uuid, p_line_id uuid, p_po_line_id uuid, p_item_id uuid,
    p_quantity numeric, p_uom text, p_condition text, p_packing_quantity numeric,
    p_notes text, p_asset_id uuid, p_asset_tag text, p_description text,
    p_owner_party_id uuid, p_custodian_party_id uuid, p_identifier_id uuid,
    p_identifier_type text, p_identifier_value text, p_unreadable_reason text,
    p_transition_id uuid
) RETURNS fleetops.receipt_lines
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $unit$
DECLARE
    trusted_org uuid := NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid;
    performer uuid;
    receipt fleetops.receipts%ROWTYPE;
    produced fleetops.receipt_lines%ROWTYPE;
BEGIN
    IF trusted_org IS NULL THEN
        RAISE EXCEPTION USING ERRCODE='42501', MESSAGE='Trusted organization required';
    END IF;
    performer := fleetops.current_authenticated_actor();
    receipt := fleetops.lock_receiving_context(trusted_org, p_receipt_id);
    IF EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations f
               WHERE f.org_id=trusted_org AND f.receipt_id=p_receipt_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Receipt population is already reconciled';
    END IF;
    -- Every target remains explicitly tenant-scoped inside this owner-executed boundary.
    IF p_owner_party_id IS NULL OR receipt.dock_location_id IS NULL OR NOT EXISTS (
        SELECT 1 FROM fleetops.parties p WHERE p.org_id=trusted_org AND p.id=p_owner_party_id
    ) OR (p_custodian_party_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fleetops.parties p WHERE p.org_id=trusted_org AND p.id=p_custodian_party_id
    )) OR NOT EXISTS (
        SELECT 1 FROM fleetops.locations l
        WHERE l.org_id=trusted_org AND l.id=receipt.dock_location_id
    ) OR NOT EXISTS (
        SELECT 1 FROM fleetops.items i
        WHERE i.org_id=trusted_org AND i.id=p_item_id AND i.serialized
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23503', MESSAGE='Invalid serialized receiving facts';
    END IF;
    -- The Python workflow selects known-conflict observation before invoking this
    -- normal boundary. Direct calls cannot use a known conflict as normal admission.
    IF p_identifier_value IS NOT NULL AND EXISTS (
        SELECT 1 FROM fleetops.asset_identifiers i WHERE i.org_id=trusted_org
          AND i.type=p_identifier_type AND i.value=p_identifier_value
    ) THEN
        RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='Readable identifier already recorded';
    END IF;
    INSERT INTO fleetops.assets
        (id,org_id,item_id,asset_tag,description,owner_party_id,custodian_party_id,
         current_location_id,current_assignment_id,current_state,version,
         created_by_actor_id,updated_by_actor_id)
    VALUES (p_asset_id,trusted_org,p_item_id,p_asset_tag,p_description,
            p_owner_party_id,p_custodian_party_id,receipt.dock_location_id,NULL,
            'RECEIVED',1,performer,performer);
    INSERT INTO fleetops.asset_transitions
        (id,org_id,asset_id,result_version,from_state,to_state,reason,actor_id,occurred_at)
    VALUES (p_transition_id,trusted_org,p_asset_id,1,NULL,'RECEIVED',
            'Physical unit received',performer,receipt.received_at);
    INSERT INTO fleetops.asset_initial_facts
        (asset_id,org_id,initial_owner_party_id,initial_custodian_party_id,
         initial_location_id,actor_id,occurred_at)
    VALUES (p_asset_id,trusted_org,p_owner_party_id,p_custodian_party_id,
            receipt.dock_location_id,performer,receipt.received_at);
    INSERT INTO fleetops.asset_initial_assignment_facts
        (asset_id,org_id,actor_id,occurred_at)
    VALUES (p_asset_id,trusted_org,performer,receipt.received_at);
    -- A concurrent claim after the precheck fails here and rolls this entire
    -- operation back. There is deliberately no uniqueness exception handler.
    INSERT INTO fleetops.asset_identifiers
        (id,org_id,asset_id,type,value,unreadable_reason,created_by_actor_id)
    VALUES (p_identifier_id,trusted_org,p_asset_id,p_identifier_type,
            p_identifier_value,p_unreadable_reason,performer);
    INSERT INTO fleetops.receipt_lines
        (id,org_id,receipt_id,po_line_id,item_id,quantity,uom,condition,
         packing_quantity,notes,owner_party_id,custodian_party_id,asset_id,actor_id)
    VALUES (p_line_id,trusted_org,p_receipt_id,p_po_line_id,p_item_id,p_quantity,
            p_uom,p_condition,p_packing_quantity,p_notes,p_owner_party_id,
            p_custodian_party_id,p_asset_id,performer)
    RETURNING * INTO produced;
    RETURN produced;
END
$unit$;
"""
INSERT_COLUMNS = {
    "receipts": "id,org_id,vendor_party_id,po_id,dock_location_id,packing_reference,received_at",
    "receipt_comparators": "id,org_id,receipt_id,po_line_id",
    "receipt_lines": "id,org_id,receipt_id,po_line_id,item_id,quantity,uom,condition,"
    "packing_quantity,notes,owner_party_id,custodian_party_id,"
    "observed_identifier_type,observed_identifier_value",
    "receipt_reconciliations": "id,org_id,receipt_id",
    "receiving_exceptions": "id,org_id,exception_type,receipt_id,receipt_line_id,"
    "po_line_id,asset_id,conflicting_asset_id",
}
INVOKERS = (
    "lock_receiving_context(uuid, uuid)",
    "enforce_receiving_record()",
    "enforce_receiving_exception()",
    "enforce_receiving_completeness()",
)


def upgrade():
    """Add only receiving-owned objects and the minimal initial history/projection boundary."""
    connection = op.get_bind()
    metadata.create_all(connection, tables=TABLES)
    connection.execute(text(GUARDS))
    connection.execute(text(CREATE_UNIT))
    for signature in (*INVOKERS, UNIT_SIGNATURE.removeprefix("fleetops.")):
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION fleetops.{signature} "
            "FROM PUBLIC, fleetops_app, fleetops_authenticator"
        )
    # The lock helper is an invoker-only RLS read. Its callers gain no object privileges.
    connection.exec_driver_sql(
        "GRANT EXECUTE ON FUNCTION fleetops.lock_receiving_context(uuid, uuid) TO fleetops_app"
    )
    connection.exec_driver_sql(f"ALTER FUNCTION {UNIT_SIGNATURE} OWNER TO fleetops_migrator")
    connection.exec_driver_sql(f"GRANT EXECUTE ON FUNCTION {UNIT_SIGNATURE} TO fleetops_app")
    for table in TABLES:
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        connection.exec_driver_sql(
            f"GRANT INSERT ({INSERT_COLUMNS[table.name]}) ON fleetops.{table.name} TO fleetops_app"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER receiving_10_guard BEFORE INSERT OR UPDATE OR DELETE "
            f"ON fleetops.{table.name} FOR EACH ROW "
            "EXECUTE FUNCTION fleetops.enforce_receiving_record()"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER receiving_30_actor AFTER INSERT ON fleetops.{table.name} "
            "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_creator('actor_id')"
        )
    # PostgreSQL requires UPDATE privilege for a row lock. The id-only grant
    # enables serialization; the immutable guard rejects every actual UPDATE.
    connection.exec_driver_sql("GRANT UPDATE (id) ON fleetops.receipts TO fleetops_app")
    connection.exec_driver_sql(
        "CREATE TRIGGER receiving_20_exception BEFORE INSERT ON fleetops.receiving_exceptions "
        "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_receiving_exception()"
    )
    for table in ("receipt_lines", "receipt_reconciliations"):
        connection.exec_driver_sql(
            f"CREATE CONSTRAINT TRIGGER receiving_40_complete AFTER INSERT ON fleetops.{table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            "EXECUTE FUNCTION fleetops.enforce_receiving_completeness()"
        )


def downgrade():
    """Remove this revision exactly; no prior Asset history or canonical identity is rewritten."""
    connection = op.get_bind()
    connection.exec_driver_sql(f"DROP FUNCTION {UNIT_SIGNATURE}")
    # The lock helper returns the receipt composite type, so remove it before its table.
    connection.exec_driver_sql("DROP FUNCTION fleetops.lock_receiving_context(uuid, uuid)")
    for table in reversed(TABLES):
        table.drop(connection, checkfirst=False)
    for signature in reversed(INVOKERS[1:]):
        connection.exec_driver_sql(f"DROP FUNCTION fleetops.{signature}")
