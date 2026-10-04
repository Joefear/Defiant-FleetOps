"""Frozen Slice 12 schema for migration 0013 (D14, D15, D18).

Never import live production schema into this historical definition.
Templates and request snapshots are immutable;
only delivery status is mutable. Printing never changes an Asset's global version.
"""

from sqlalchemy import (
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


def define_label_tables(metadata):
    """Keep references, authenticated attribution and retry state in the same tenant."""

    def relation(name, *columns):
        return Table(
            name,
            metadata,
            Column("id", Uuid, primary_key=True),
            Column("org_id", Uuid, nullable=False),
            *columns,
            UniqueConstraint("org_id", "id", name=f"uq_{name}_org_id"),
            ForeignKeyConstraint(["org_id"], ["fleetops.organizations.id"], name=f"fk_{name}_org"),
        )

    def reference(table, column, target):
        table.append_constraint(
            ForeignKeyConstraint(
                ["org_id", column],
                [f"fleetops.{target}.org_id", f"fleetops.{target}.id"],
                name=f"fk_{table.name}_{column}",
            )
        )
        Index(f"ix_{table.name}_{column}", table.c.org_id, table.c[column])

    templates = relation(
        "label_templates",
        Column("name", Text, nullable=False),
        Column("entity_type", Text, nullable=False),
        Column("human_fields", JSONB, nullable=False),
        Column("symbology", Text, nullable=False),
        Column("barcode_field", Text, nullable=False, server_default=text("'id'")),
        Column(
            "created_by_actor_id",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column(
            "created_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 100 AND name !~ '[[:cntrl:]]'",
            name="ck_label_templates_name",
        ),
        CheckConstraint("entity_type = 'ASSET'", name="ck_label_templates_entity"),
        CheckConstraint(
            "symbology IN ('CODE128','DATAMATRIX')", name="ck_label_templates_symbology"
        ),
        CheckConstraint("barcode_field = 'id'", name="ck_label_templates_identity_only"),
        CheckConstraint(
            "jsonb_typeof(human_fields) = 'array' AND "
            "jsonb_array_length(human_fields) BETWEEN 1 AND 4 AND "
            """human_fields <@ '["id","asset_tag","description","item_mpn"]'::jsonb""",
            name="ck_label_templates_human_fields",
        ),
        CheckConstraint("isfinite(created_at)", name="ck_label_templates_time"),
    )
    reference(templates, "created_by_actor_id", "actors")
    jobs = relation(
        "print_jobs",
        Column("template_id", Uuid, nullable=False),
        Column("entity_type", Text, nullable=False, server_default=text("'ASSET'")),
        Column("entity_id", Uuid, nullable=False),
        Column(
            "requested_by",
            Uuid,
            nullable=False,
            server_default=text("fleetops.current_authenticated_actor()"),
        ),
        Column(
            "requested_at",
            DateTime(timezone=True),
            nullable=False,
            server_default=text("statement_timestamp()"),
        ),
        Column("status", Text, nullable=False, server_default=text("'PENDING'")),
        Column("adapter_name", Text, nullable=False),
        Column("output_format", Text, nullable=False),
        Column("render_snapshot", JSONB, nullable=False),
        Column("attempts", Integer, nullable=False, server_default=text("0")),
        Column("artifact_sha256", Text),
        Column("error_code", Text),
        Column("updated_by_actor_id", Uuid),
        Column("updated_at", DateTime(timezone=True)),
        CheckConstraint("entity_type = 'ASSET'", name="ck_print_jobs_entity"),
        CheckConstraint("status IN ('PENDING','SUCCEEDED','FAILED')", name="ck_print_jobs_status"),
        CheckConstraint("output_format IN ('PNG','PDF','ZPL')", name="ck_print_jobs_format"),
        CheckConstraint(
            "(adapter_name = 'FILE' AND output_format IN ('PNG','PDF')) OR "
            "(adapter_name = 'ZPL' AND output_format = 'ZPL')",
            name="ck_print_jobs_adapter",
        ),
        CheckConstraint(
            "jsonb_typeof(render_snapshot) = 'object' AND render_snapshot->>'id' = entity_id::text",
            name="ck_print_jobs_snapshot",
        ),
        CheckConstraint(
            "(status = 'PENDING' AND attempts = 0 AND artifact_sha256 IS NULL "
            "AND error_code IS NULL AND updated_by_actor_id IS NULL AND updated_at IS NULL) OR "
            "(status IN ('SUCCEEDED','FAILED') AND attempts > 0 "
            "AND updated_by_actor_id IS NOT NULL AND updated_at IS NOT NULL AND "
            "((status = 'SUCCEEDED' AND artifact_sha256 IS NOT NULL "
            "AND artifact_sha256 ~ '^[0-9a-f]{64}$' AND error_code IS NULL) OR "
            "(status = 'FAILED' AND artifact_sha256 IS NULL AND error_code IS NOT NULL "
            "AND error_code IN ('OUTPUT_UNAVAILABLE','RENDER_FAILED'))))",
            name="ck_print_jobs_delivery",
        ),
        CheckConstraint(
            "isfinite(requested_at) AND (updated_at IS NULL OR isfinite(updated_at))",
            name="ck_print_jobs_time",
        ),
    )
    reference(jobs, "template_id", "label_templates")
    reference(jobs, "entity_id", "assets")
    reference(jobs, "requested_by", "actors")
    reference(jobs, "updated_by_actor_id", "actors")
    Index("ix_print_jobs_pending", jobs.c.org_id, jobs.c.status, jobs.c.id)
    return templates, jobs
