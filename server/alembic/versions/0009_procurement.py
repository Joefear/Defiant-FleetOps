"""Procurement expectation and immutable line succession (D5, D13, ADR-009).

Revision ID: 0009_procurement
Revises: 0008_assignment_configuration

The migration snapshots its own schema and vocabulary. No prior histories, grants,
functions, or current Asset projections are changed.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Computed,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
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

revision = "0009_procurement"
down_revision = "0008_assignment_configuration"
branch_labels = None
depends_on = None
metadata = MetaData(schema="fleetops")
Table("organizations", metadata, Column("id", Uuid))
for name in ("actors", "parties", "items"):
    Table(name, metadata, Column("org_id", Uuid), Column("id", Uuid))
Table(
    "party_roles", metadata, Column("org_id", Uuid), Column("party_id", Uuid), Column("role", Text)
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
        "status IN ("
        + ", ".join(repr(v) for v in ("DRAFT", "ISSUED", "CLOSED", "CANCELLED"))
        + ")",
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
        "uom IN ("
        + ", ".join(
            repr(v) for v in ("EA", "M", "MM", "CM", "IN", "FT", "G", "MG", "KG", "ML", "L")
        )
        + ")",
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


GUARDS = """
-- Both guards are invokers: ordinary RLS access and existing ADR-006 credential
-- resolution suffice. They neither acquire authentication-table authority nor
-- mutate a predecessor. All line writes hold the same PO lock through commit.
CREATE FUNCTION fleetops.enforce_purchase_order()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $po$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'DRAFT' OR OLD.issued_at IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Issued purchase order is immutable';
        END IF;
        RETURN OLD;
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IS DISTINCT FROM 'DRAFT' OR NEW.issued_at IS NOT NULL
           OR NEW.issued_by_actor_id IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Purchase order must begin DRAFT';
        END IF;
        NEW.created_at := pg_catalog.statement_timestamp();
        NEW.updated_at := NEW.created_at;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'DRAFT' OR OLD.issued_at IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Issued purchase order is immutable';
    END IF;
    IF ROW(NEW.id, NEW.org_id, NEW.created_by_actor_id, NEW.created_at,
           NEW.issued_at, NEW.issued_by_actor_id)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.org_id, OLD.created_by_actor_id, OLD.created_at,
           OLD.issued_at, OLD.issued_by_actor_id) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Purchase order identity or attribution is immutable';
    END IF;
    IF NEW.status NOT IN ('DRAFT', 'ISSUED') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Purchase order transition is unavailable';
    END IF;
    IF NEW.status = 'ISSUED' THEN
        NEW.issued_at := pg_catalog.statement_timestamp();
        IF session_user = 'fleetops_app' THEN
            NEW.issued_by_actor_id := fleetops.current_authenticated_actor();
        ELSE
            -- Independent deployment fixtures remain explicit administrative setup.
            NEW.issued_by_actor_id := NEW.updated_by_actor_id;
        END IF;
    END IF;
    RETURN NEW;
END
$po$;

CREATE FUNCTION fleetops.enforce_purchase_order_line()
RETURNS trigger LANGUAGE plpgsql VOLATILE SECURITY INVOKER
SET search_path = pg_catalog, pg_temp
AS $line$
DECLARE
    target_org uuid;
    target_po uuid;
    po_status text;
    po_issued_at timestamptz;
BEGIN
    IF TG_OP = 'INSERT' THEN
        target_org := NEW.org_id;
        target_po := NEW.po_id;
    ELSE
        target_org := OLD.org_id;
        target_po := OLD.po_id;
    END IF;
    -- NO KEY UPDATE serializes authoring/issuance/amendments without deadlocking
    -- ordinary FK KEY SHARE checks. PO UPDATE itself takes a conflicting row lock.
    SELECT p.status, p.issued_at INTO po_status, po_issued_at
      FROM fleetops.purchase_orders p
      WHERE p.org_id = target_org AND p.id = target_po FOR NO KEY UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Purchase order not found';
    END IF;
    IF TG_OP <> 'INSERT' THEN
        IF po_status <> 'DRAFT' OR po_issued_at IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Issued line is immutable';
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        IF ROW(NEW.id, NEW.org_id, NEW.po_id, NEW.line_number, NEW.supersedes_line_id,
               NEW.uom, NEW.created_by_actor_id, NEW.created_at)
           IS DISTINCT FROM
           ROW(OLD.id, OLD.org_id, OLD.po_id, OLD.line_number, OLD.supersedes_line_id,
               OLD.uom, OLD.created_by_actor_id, OLD.created_at) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Line identity, lineage, or UOM is immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.supersedes_line_id IS NULL THEN
        IF po_status <> 'DRAFT' OR po_issued_at IS NOT NULL THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Root lines require a DRAFT purchase order';
        END IF;
    ELSE
        IF po_status <> 'ISSUED' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'Amendments require an ISSUED purchase order';
        END IF;
        -- BEFORE ROW sees established predecessors but cannot see a later row in
        -- the same INSERT. The first forward edge of any attempted cycle rejects.
        -- Immutable pointers and the same-PO lock make this an append-only graph.
        IF NEW.id = NEW.supersedes_line_id OR NOT EXISTS (
            SELECT 1 FROM fleetops.purchase_order_lines prior
            WHERE prior.org_id = NEW.org_id AND prior.po_id = NEW.po_id
              AND prior.line_number = NEW.line_number AND prior.id = NEW.supersedes_line_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Invalid predecessor relationship';
        END IF;
        IF EXISTS (
            SELECT 1 FROM fleetops.purchase_order_lines successor
            WHERE successor.org_id = NEW.org_id
              AND successor.supersedes_line_id = NEW.supersedes_line_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'Predecessor is no longer active';
        END IF;
    END IF;
    -- D13: capture the selected Item default once; subsequent catalog edits and
    -- draft Item edits never refresh this row's historical UOM.
    SELECT i.uom INTO NEW.uom FROM fleetops.items i
      WHERE i.org_id = NEW.org_id AND i.id = NEW.item_id FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION USING ERRCODE = '23503', MESSAGE = 'Item not found';
    END IF;
    NEW.created_at := pg_catalog.statement_timestamp();
    NEW.updated_at := NEW.created_at;
    RETURN NEW;
END
$line$;
"""

INSERT_COLUMNS = {
    "purchase_orders": (
        "id, org_id, vendor_party_id, po_number, notes, created_by_actor_id, updated_by_actor_id"
    ),
    "purchase_order_lines": (
        "id, org_id, po_id, line_number, item_id, quantity, unit_price, "
        "expected_date, supersedes_line_id, created_by_actor_id, "
        "updated_by_actor_id"
    ),
}
UPDATE_COLUMNS = {
    "purchase_orders": "vendor_party_id, po_number, notes, status, updated_by_actor_id, updated_at",
    "purchase_order_lines": (
        "item_id, quantity, unit_price, expected_date, updated_by_actor_id, updated_at"
    ),
}


def upgrade() -> None:
    """Add only expectation tables and invoker invariants; preserve every prior ACL."""
    connection = op.get_bind()
    metadata.create_all(connection, tables=[purchase_orders, purchase_order_lines])
    connection.exec_driver_sql(GUARDS)
    for function in ("enforce_purchase_order", "enforce_purchase_order_line"):
        connection.exec_driver_sql(
            f"REVOKE ALL ON FUNCTION fleetops.{function}() "
            "FROM PUBLIC, fleetops_app, fleetops_authenticator"
        )
    for table, guard in (
        (purchase_orders, "enforce_purchase_order"),
        (purchase_order_lines, "enforce_purchase_order_line"),
    ):
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        name = table.name
        connection.exec_driver_sql(
            f"GRANT INSERT ({INSERT_COLUMNS[name]}) ON fleetops.{name} TO fleetops_app"
        )
        connection.exec_driver_sql(
            f"GRANT UPDATE ({UPDATE_COLUMNS[name]}) ON fleetops.{name} TO fleetops_app"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER procurement_10_guard BEFORE INSERT OR UPDATE OR DELETE "
            f"ON fleetops.{name} FOR EACH ROW EXECUTE FUNCTION fleetops.{guard}()"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER procurement_20_creator BEFORE INSERT ON fleetops.{name} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "fleetops.enforce_authenticated_creator('created_by_actor_id')"
        )
        connection.exec_driver_sql(
            f"CREATE TRIGGER procurement_30_updater BEFORE UPDATE ON fleetops.{name} "
            "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_updater()"
        )


def downgrade() -> None:
    """Remove exactly this revision's tables, dependent guards, and indexes."""
    connection = op.get_bind()
    purchase_order_lines.drop(connection)
    purchase_orders.drop(connection)
    connection.exec_driver_sql("DROP FUNCTION fleetops.enforce_purchase_order_line()")
    connection.exec_driver_sql("DROP FUNCTION fleetops.enforce_purchase_order()")
