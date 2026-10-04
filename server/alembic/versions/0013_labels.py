"""Tenant-scoped immutable label templates and a durable local-print outbox.

Revision ID: 0013_labels
Revises: 0012_evidence
"""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, Uuid, text

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0013_labels"
down_revision = "0012_evidence"
branch_labels = None
depends_on = None

VALIDATION_SQL = """
CREATE FUNCTION fleetops.guard_label_template() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $template$
BEGIN
  IF session_user='fleetops_app' THEN
    NEW.created_by_actor_id := fleetops.current_authenticated_actor();
    NEW.created_at := statement_timestamp();
  END IF;
  RETURN NEW;
END $template$;
REVOKE ALL ON FUNCTION fleetops.guard_label_template()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;

CREATE FUNCTION fleetops.guard_print_job() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $job$
DECLARE performer uuid; captured jsonb;
BEGIN
  IF TG_OP='INSERT' THEN
    -- The snapshot is always derived from actual tenant-owned records. Supplied JSON
    -- cannot smuggle another identity or a configurable barcode expression into it.
    SELECT jsonb_build_object(
      'id',a.id::text,'asset_tag',a.asset_tag,'description',a.description,
      'item_mpn',i.manufacturer_part_number,'human_fields',t.human_fields,'symbology',t.symbology)
      INTO captured
      FROM fleetops.assets a JOIN fleetops.items i
        ON i.org_id=a.org_id AND i.id=a.item_id
      JOIN fleetops.label_templates t ON t.org_id=a.org_id AND t.id=NEW.template_id
      WHERE a.org_id=NEW.org_id AND a.id=NEW.entity_id;
    IF captured IS NULL THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid print target or template';
    END IF;
    NEW.render_snapshot:=captured;
    NEW.entity_type:='ASSET';
    IF NEW.status<>'PENDING' OR NEW.attempts<>0 OR NEW.artifact_sha256 IS NOT NULL
      OR NEW.error_code IS NOT NULL OR NEW.updated_at IS NOT NULL
      OR NEW.updated_by_actor_id IS NOT NULL THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='New print job must be pending';
    END IF;
    IF session_user='fleetops_app' THEN
      NEW.requested_by:=fleetops.current_authenticated_actor();
      NEW.requested_at:=statement_timestamp();
    END IF;
  ELSE
    -- Only delivery state changes. A print retry must never rewrite its request.
    IF (to_jsonb(NEW)-ARRAY['status','artifact_sha256','error_code',
       'attempts','updated_by_actor_id','updated_at']) IS DISTINCT FROM
       (to_jsonb(OLD)-ARRAY['status','artifact_sha256','error_code',
       'attempts','updated_by_actor_id','updated_at']) THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Print request is immutable';
    END IF;
    IF OLD.status='SUCCEEDED' OR NEW.status NOT IN ('SUCCEEDED','FAILED') THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid print delivery transition';
    END IF;
    performer:=fleetops.current_authenticated_actor();
    NEW.attempts:=OLD.attempts+1;
    NEW.updated_by_actor_id:=performer;
    NEW.updated_at:=statement_timestamp();
  END IF;
  RETURN NEW;
END $job$;
REVOKE ALL ON FUNCTION fleetops.guard_print_job()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;
"""


def frozen_tables():
    """A future live schema edit cannot rewrite the meaning of this migration."""
    source = Path(__file__).resolve().parent.parent / "label_schema_0013.py"
    spec = importlib.util.spec_from_file_location("fleetops_labels_0013", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = MetaData(schema="fleetops")
    for name in ("organizations", "actors", "assets"):
        Table(name, metadata, Column("id", Uuid), Column("org_id", Uuid))
    return module.define_label_tables(metadata)


def upgrade():
    """Use ordinary RLS-protected DML; no new SECURITY DEFINER boundary is introduced."""
    connection = op.get_bind()
    templates, jobs = frozen_tables()
    for table in (templates, jobs):
        table.create(connection)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
    connection.execute(text(VALIDATION_SQL))
    connection.exec_driver_sql("""
      GRANT INSERT (id,org_id,name,entity_type,human_fields,symbology,barcode_field)
        ON fleetops.label_templates TO fleetops_app;
      GRANT INSERT (id,org_id,template_id,entity_id,adapter_name,output_format)
        ON fleetops.print_jobs TO fleetops_app;
      GRANT UPDATE (status,artifact_sha256,error_code)
        ON fleetops.print_jobs TO fleetops_app;
      CREATE TRIGGER label_template_attribution BEFORE INSERT ON fleetops.label_templates
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_label_template();
      CREATE TRIGGER print_job_capture BEFORE INSERT OR UPDATE ON fleetops.print_jobs
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_print_job();
    """)


def downgrade():
    """Never erase accepted templates or print observations through schema rollback."""
    connection = op.get_bind()
    present = connection.exec_driver_sql(
        "SELECT EXISTS(SELECT 1 FROM fleetops.label_templates) "
        "OR EXISTS(SELECT 1 FROM fleetops.print_jobs)"
    ).scalar_one()
    if present:
        raise RuntimeError("Cannot downgrade accepted label records")
    connection.exec_driver_sql("""
      DROP TABLE fleetops.print_jobs;
      DROP TABLE fleetops.label_templates;
      DROP FUNCTION fleetops.guard_print_job();
      DROP FUNCTION fleetops.guard_label_template();
    """)
