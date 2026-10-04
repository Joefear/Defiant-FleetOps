"""Authenticated idempotent capture and append-only sync conflicts.

Revision ID: 0014_capture
Revises: 0013_labels
"""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, Table, Uuid, text

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0014_capture"
down_revision = "0013_labels"
branch_labels = None
depends_on = None

VALIDATION_SQL = """

CREATE FUNCTION fleetops.capture_asset_facts(p_asset uuid,p_version integer) RETURNS jsonb
LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $facts$
SELECT CASE WHEN p_version>a.version THEN
  jsonb_build_object('version',p_version,'known_version',false)
ELSE jsonb_build_object('version',p_version,'known_version',true,
  'state',(SELECT to_state FROM fleetops.asset_transitions h
    WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version
    ORDER BY h.result_version DESC LIMIT 1),
  'location_id',CASE WHEN EXISTS(SELECT 1 FROM fleetops.asset_movements h
    WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version)
    THEN (SELECT to_location_id FROM fleetops.asset_movements h
      WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version
      ORDER BY h.result_version DESC LIMIT 1)
    ELSE (SELECT initial_location_id FROM fleetops.asset_initial_facts b
      WHERE b.org_id=a.org_id AND b.asset_id=a.id) END,
  'custodian_party_id',CASE WHEN EXISTS(SELECT 1 FROM fleetops.asset_custody_changes h
    WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version)
    THEN (SELECT to_custodian_party_id FROM fleetops.asset_custody_changes h
      WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version
      ORDER BY h.result_version DESC LIMIT 1)
    ELSE (SELECT initial_custodian_party_id FROM fleetops.asset_initial_facts b
      WHERE b.org_id=a.org_id AND b.asset_id=a.id) END,
  'owner_party_id',COALESCE(
    (SELECT to_owner_party_id FROM fleetops.asset_ownership_changes h
      WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version
      ORDER BY h.result_version DESC LIMIT 1),
    (SELECT initial_owner_party_id FROM fleetops.asset_initial_facts b
      WHERE b.org_id=a.org_id AND b.asset_id=a.id)),
  'assignee_id',(SELECT to_assignee_id FROM fleetops.asset_assignment_events h
    WHERE h.org_id=a.org_id AND h.asset_id=a.id AND h.result_version<=p_version
    ORDER BY h.result_version DESC LIMIT 1)) END
FROM fleetops.assets a WHERE a.id=p_asset
AND a.org_id=NULLIF(current_setting('fleetops.org_id',true),'')::uuid
$facts$;
REVOKE ALL ON FUNCTION fleetops.capture_asset_facts(uuid,integer)
  FROM PUBLIC,fleetops_app,fleetops_authenticator;
GRANT EXECUTE ON FUNCTION fleetops.capture_asset_facts(uuid,integer) TO fleetops_app;

CREATE FUNCTION fleetops.guard_sync_conflict_origin() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $origin$
DECLARE captured fleetops.capture_operations%ROWTYPE;
BEGIN
  SELECT * INTO captured FROM fleetops.capture_operations
    WHERE org_id=NEW.org_id AND operation_id=NEW.operation_id;
  IF captured.operation_id IS NULL OR captured.sync_state<>'REJECTED'
     OR captured.actor_id<>NEW.actor_id OR captured.claimed_actor_id<>NEW.actor_id
     OR captured.entity_type<>'ASSET' OR captured.entity_id<>NEW.asset_id
     OR captured.expected_version<>NEW.expected_version
     OR captured.operation NOT IN ('MOVE','ASSIGN','UNASSIGN','TRANSITION')
     OR captured.occurred_at<>NEW.occurred_at
     OR captured.result->>'code' IS DISTINCT FROM 'SYNC_CONFLICT'
     OR captured.result->>'exception_id' IS DISTINCT FROM NEW.id::text
     OR captured.result->'expected' IS DISTINCT FROM NEW.expected_facts
     OR captured.result->'current' IS DISTINCT FROM NEW.current_facts THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Conflict must match its rejected operation';
  END IF;
  RETURN NULL;
END $origin$;
REVOKE ALL ON FUNCTION fleetops.guard_sync_conflict_origin()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;

CREATE FUNCTION fleetops.guard_capture_stream() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $stream$
DECLARE performer uuid;
BEGIN
  performer:=fleetops.current_authenticated_actor();
  IF TG_OP='INSERT' THEN
    NEW.actor_id:=performer;
    IF NEW.last_seq<>0 THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='New stream must begin at zero';
    END IF;
  ELSE
    IF (NEW.org_id,NEW.client_id,NEW.client_epoch,NEW.actor_id) IS DISTINCT FROM
       (OLD.org_id,OLD.client_id,OLD.client_epoch,OLD.actor_id)
       OR performer<>OLD.actor_id OR NEW.last_seq<OLD.last_seq THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid capture stream change';
    END IF;
  END IF;
  RETURN NEW;
END $stream$;

CREATE FUNCTION fleetops.guard_capture_record() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $record$
BEGIN
  NEW.actor_id:=fleetops.current_authenticated_actor();
  NEW.recorded_at:=statement_timestamp();
  IF NEW.sync_state='APPLIED' AND NEW.claimed_actor_id<>NEW.actor_id THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Applied capture requires matching Actor';
  END IF;
  RETURN NEW;
END $record$;

CREATE FUNCTION fleetops.guard_sync_conflict() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $conflict$
DECLARE actual integer;
BEGIN
  NEW.actor_id:=fleetops.current_authenticated_actor();
  NEW.recorded_at:=statement_timestamp();
  SELECT version INTO actual FROM fleetops.assets
    WHERE org_id=NEW.org_id AND id=NEW.asset_id FOR UPDATE;
  IF actual IS NULL OR actual<>NEW.current_version OR actual=NEW.expected_version THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Sync conflict requires current mismatch';
  END IF;
  IF NEW.expected_facts IS DISTINCT FROM
     fleetops.capture_asset_facts(NEW.asset_id,NEW.expected_version)
     OR NEW.current_facts IS DISTINCT FROM fleetops.capture_asset_facts(NEW.asset_id,actual) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Sync facts must match immutable history';
  END IF;
  RETURN NEW;
END $conflict$;

CREATE FUNCTION fleetops.guard_sync_conflict_event() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $event$
DECLARE prior_status text; prior_seq integer; target uuid;
BEGIN
  NEW.actor_id:=fleetops.current_authenticated_actor();
  NEW.recorded_at:=statement_timestamp();
  PERFORM pg_advisory_xact_lock(hashtextextended('sync-conflict:'||NEW.exception_id::text,0));
  SELECT id INTO target FROM fleetops.sync_conflicts
    WHERE org_id=NEW.org_id AND id=NEW.exception_id;
  IF target IS NULL THEN
    RAISE EXCEPTION USING ERRCODE='P0002',MESSAGE='Exception not found';
  END IF;
  SELECT to_status,event_seq INTO prior_status,prior_seq
    FROM fleetops.sync_conflict_events
    WHERE org_id=NEW.org_id AND exception_id=target ORDER BY event_seq DESC LIMIT 1;
  IF NEW.from_status<>COALESCE(prior_status,'OPEN')
     OR NEW.event_seq<>COALESCE(prior_seq,0)+1 THEN
    RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Exception status changed';
  END IF;
  NEW.note:=fleetops.normalize_correction_reason(NEW.note);
  RETURN NEW;
END $event$;
REVOKE ALL ON FUNCTION fleetops.guard_capture_stream(),fleetops.guard_capture_record(),
  fleetops.guard_sync_conflict(),fleetops.guard_sync_conflict_event()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;
"""


def frozen_tables():
    """Load independent definitions so future live mappings cannot change this revision."""
    source = Path(__file__).resolve().parent.parent / "capture_schema_0014.py"
    spec = importlib.util.spec_from_file_location("fleetops_capture_0014", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    metadata = MetaData(schema="fleetops")
    for name in ("organizations", "actors", "assets"):
        Table(name, metadata, Column("id", Uuid), Column("org_id", Uuid))
    return module.define_capture_tables(metadata)


def upgrade():
    """Ordinary RLS-protected writes; no new elevated execution boundary."""
    connection = op.get_bind()
    tables = frozen_tables()
    for table in tables:
        table.create(connection)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
    connection.execute(text(VALIDATION_SQL))
    connection.exec_driver_sql("""
      GRANT INSERT(org_id,client_id,client_epoch) ON fleetops.capture_streams TO fleetops_app;
      GRANT UPDATE(last_seq) ON fleetops.capture_streams TO fleetops_app;
      GRANT INSERT(operation_id,org_id,claimed_actor_id,client_id,client_epoch,client_seq,
        entity_type,entity_id,expected_version,operation,payload,occurred_at,
        sync_state,result,sequence_flags) ON fleetops.capture_operations TO fleetops_app;
      GRANT INSERT(id,org_id,operation_id,asset_id,expected_version,current_version,
        expected_facts,current_facts,occurred_at) ON fleetops.sync_conflicts TO fleetops_app;
      GRANT INSERT(id,org_id,exception_id,event_seq,from_status,to_status,note,occurred_at)
        ON fleetops.sync_conflict_events TO fleetops_app;
      CREATE TRIGGER capture_stream_guard BEFORE INSERT OR UPDATE ON fleetops.capture_streams
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_capture_stream();
      CREATE TRIGGER capture_record_guard BEFORE INSERT ON fleetops.capture_operations
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_capture_record();
      CREATE TRIGGER sync_conflict_guard BEFORE INSERT ON fleetops.sync_conflicts
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_sync_conflict();
      CREATE CONSTRAINT TRIGGER sync_conflict_origin AFTER INSERT ON fleetops.sync_conflicts
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION fleetops.guard_sync_conflict_origin();
      CREATE TRIGGER sync_conflict_event_guard BEFORE INSERT ON fleetops.sync_conflict_events
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_sync_conflict_event();
    """)


def downgrade():
    """An empty disposable schema can roll back; accepted capture history cannot."""
    connection = op.get_bind()
    tables = frozen_tables()
    if any(
        connection.exec_driver_sql(
            f"SELECT EXISTS(SELECT 1 FROM fleetops.{table.name})"
        ).scalar_one()
        for table in tables
    ):
        raise RuntimeError("Cannot downgrade accepted capture records")
    for table in reversed(tables):
        table.drop(connection)
    connection.exec_driver_sql("DROP FUNCTION fleetops.capture_asset_facts(uuid,integer)")
    for name in (
        "guard_sync_conflict_origin",
        "guard_capture_stream",
        "guard_capture_record",
        "guard_sync_conflict",
        "guard_sync_conflict_event",
    ):
        connection.exec_driver_sql(f"DROP FUNCTION fleetops.{name}()")
