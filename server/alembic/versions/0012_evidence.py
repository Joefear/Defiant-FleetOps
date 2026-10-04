"""Immutable evidence and the previously reserved evidence-reference seams.

Revision ID: 0012_evidence
Revises: 0011_corrections

Python owns file I/O and lifecycle decisions. Ordinary RLS-protected INSERTs capture
metadata/links; invoker triggers enforce provenance and evidence relationships. No
new elevated privilege boundary is introduced. Frozen predecessor SQL is adapted
forward and restored exactly by downgrade, which refuses to discard accepted evidence.
"""

from sqlalchemy import Column, MetaData, Table, Uuid, text

from alembic import op
from fleetops.db.tenancy import apply_tenant_policy

revision = "0012_evidence"
down_revision = "0011_corrections"
branch_labels = None
depends_on = None


def predecessor():
    """Load the committed 0011 generator without importing current domain behavior."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "evidence_predecessor", Path(__file__).with_name("0011_corrections.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ordinary_transition_sql(previous):
    """Reproduce the exact 0011 ordinary transition, including its anomaly gate."""
    sql = previous.ordinary_functions()[0].replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    )
    for _, history, *_ in previous.HISTORIES:
        sql = sql.replace(f"FROM fleetops.{history} ", f"FROM fleetops.effective_{history} ")
    for alias in ("h", "t"):
        sql = sql.replace(
            f"    SELECT {alias}.* INTO latest",
            "    IF EXISTS (SELECT 1 FROM fleetops.asset_correction_anomalies "
            "WHERE asset_id=p_asset_id) THEN RAISE EXCEPTION USING "
            "ERRCODE='P0001',MESSAGE='Malformed correction authority'; END IF;\n"
            f"    SELECT {alias}.* INTO latest",
        )
    return sql


def replaced(sql, old, new, count=1):
    """Fail migration if a frozen adaptation anchor no longer means what it did."""
    if sql.count(old) != count:
        raise RuntimeError("Frozen evidence migration SQL anchor changed")
    return sql.replace(old, new)


VALIDATION_SQL = """
CREATE FUNCTION fleetops.valid_asset_evidence(p_org uuid,p_ref uuid,p_asset uuid,p_role text)
RETURNS boolean LANGUAGE sql STABLE SECURITY INVOKER SET search_path=pg_catalog,pg_temp
AS $evidence$
  SELECT EXISTS (
    SELECT 1 FROM fleetops.attachments a
    JOIN fleetops.actors capturer ON capturer.org_id=a.org_id AND capturer.id=a.captured_by
    JOIN fleetops.attachment_links l ON l.org_id=a.org_id AND l.attachment_id=a.id
    JOIN fleetops.actors linker ON linker.org_id=l.org_id AND linker.id=l.actor_id
    WHERE a.org_id=p_org AND a.id=p_ref
      AND l.entity_type='ASSET' AND l.entity_id=p_asset
      AND (p_role IS NULL OR l.link_role=p_role)
      AND isfinite(a.captured_at) AND isfinite(a.recorded_at) AND isfinite(l.recorded_at)
      AND a.sha256 ~ '^[0-9a-f]{64}$' AND a.byte_size>=0
      AND a.storage_key=a.org_id::text || '/' || a.sha256
      AND length(btrim(a.original_filename)) BETWEEN 1 AND 255
      AND a.source_type IN ('PHOTO','DOCUMENT','SCAN','REPORT','CERTIFICATE','OTHER')
      AND a.media_type ~ '^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$'
      AND length(a.media_type)<=127
  )
$evidence$;
REVOKE ALL ON FUNCTION fleetops.valid_asset_evidence(uuid,uuid,uuid,text)
  FROM PUBLIC,fleetops_app,fleetops_authenticator;
GRANT EXECUTE ON FUNCTION fleetops.valid_asset_evidence(uuid,uuid,uuid,text) TO fleetops_app;

CREATE FUNCTION fleetops.guard_evidence_capture() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $capture$
DECLARE performer uuid; target_exists boolean;
BEGIN
  -- session_user still identifies runtime calls inside an elevated operation.
  -- A disabled capturer later does not erase their earlier authenticated evidence.
  IF session_user='fleetops_app' THEN
    performer:=fleetops.current_authenticated_actor();
    IF TG_TABLE_NAME='attachments' THEN
      IF NEW.captured_by IS DISTINCT FROM performer THEN
        RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Invalid evidence capturer'; END IF;
    ELSE
      IF NEW.actor_id IS DISTINCT FROM performer THEN
        RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Invalid evidence linker'; END IF;
    END IF;
    NEW.recorded_at:=statement_timestamp();
  END IF;
  IF TG_TABLE_NAME='attachment_links' THEN
    -- Only fixed existing entity classes are admitted; payload text never becomes SQL.
    target_exists:=CASE NEW.entity_type
      WHEN 'ASSET' THEN EXISTS (SELECT 1 FROM fleetops.assets
        WHERE org_id=NEW.org_id AND id=NEW.entity_id)
      WHEN 'RECEIPT' THEN EXISTS (SELECT 1 FROM fleetops.receipts
        WHERE org_id=NEW.org_id AND id=NEW.entity_id)
      WHEN 'RECEIPT_LINE' THEN EXISTS (SELECT 1 FROM fleetops.receipt_lines
        WHERE org_id=NEW.org_id AND id=NEW.entity_id)
      WHEN 'ASSET_CONFIGURATION' THEN EXISTS (SELECT 1 FROM fleetops.asset_configurations
        WHERE org_id=NEW.org_id AND id=NEW.entity_id)
      WHEN 'RECEIVING_EXCEPTION' THEN EXISTS (SELECT 1 FROM fleetops.receiving_exceptions
        WHERE org_id=NEW.org_id AND id=NEW.entity_id)
      ELSE false END;
    IF NOT target_exists OR
      (NEW.link_role='DISPOSAL_EVIDENCE' AND NEW.entity_type<>'ASSET') OR
      (NEW.link_role='CONFIG_EVIDENCE'
        AND NEW.entity_type NOT IN ('ASSET','ASSET_CONFIGURATION')) OR
      (NEW.link_role='RECEIVING_EVIDENCE'
        AND NEW.entity_type NOT IN ('ASSET','RECEIPT','RECEIPT_LINE')) OR
      (NEW.link_role='EXCEPTION_EVIDENCE' AND NEW.entity_type<>'RECEIVING_EXCEPTION') THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid evidence target or role'; END IF;
  END IF;
  RETURN NEW;
END $capture$;
REVOKE ALL ON FUNCTION fleetops.guard_evidence_capture()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;

CREATE FUNCTION fleetops.guard_asset_evidence() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $reference$
DECLARE purpose text;
BEGIN
  IF TG_TABLE_NAME='asset_configurations' THEN purpose:='CONFIG_EVIDENCE';
  ELSIF NEW.to_state='RETIRED' AND NEW.correction_role<>'REVERSAL' THEN
    purpose:='DISPOSAL_EVIDENCE';
  END IF;
  IF (NEW.evidence_ref IS NOT NULL OR purpose='DISPOSAL_EVIDENCE') AND
    NOT fleetops.valid_asset_evidence(NEW.org_id,NEW.evidence_ref,NEW.asset_id,purpose) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Verifiable linked evidence required';
  END IF;
  RETURN NEW;
END $reference$;
REVOKE ALL ON FUNCTION fleetops.guard_asset_evidence()
  FROM PUBLIC,fleetops_app,fleetops_authenticator;
"""

EVIDENCE_ANOMALIES = """
  UNION ALL
  SELECT h.asset_id FROM fleetops.asset_transitions h
  WHERE (h.evidence_ref IS NOT NULL OR
      (h.to_state='RETIRED' AND h.correction_role<>'REVERSAL'))
    AND NOT fleetops.valid_asset_evidence(h.org_id,h.evidence_ref,h.asset_id,
      CASE WHEN h.to_state='RETIRED' AND h.correction_role<>'REVERSAL'
        THEN 'DISPOSAL_EVIDENCE' END)
  UNION ALL
  SELECT h.asset_id FROM fleetops.asset_transitions h
  JOIN fleetops.asset_transitions p ON p.org_id=h.org_id AND p.asset_id=h.asset_id
    AND ((h.correction_generation=1 AND p.id=h.corrects_transition_id) OR
      (h.correction_generation>1 AND p.corrects_transition_id=h.corrects_transition_id
       AND p.correction_generation=h.correction_generation-1 AND p.correction_role='CORRECTED'))
  WHERE h.correction_role='REVERSAL' AND h.evidence_ref IS DISTINCT FROM p.evidence_ref
"""


def upgrade():
    """Create immutable tenant records, then replace only the approved evidence seams."""
    connection = op.get_bind()
    previous = predecessor()
    frozen = previous.historical("../evidence_schema_0012.py")
    metadata = MetaData(schema="fleetops")
    for name in ("organizations", "actors"):
        Table(name, metadata, Column("id", Uuid), Column("org_id", Uuid))
    tables = frozen.define_evidence_tables(metadata)
    for table in tables:
        table.create(connection)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        columns = [
            c.name for c in table.c if c.name not in ("captured_by", "actor_id", "recorded_at")
        ]
        connection.execute(
            text(f"GRANT INSERT ({','.join(columns)}) ON fleetops.{table.name} TO fleetops_app")
        )
    connection.execute(text(VALIDATION_SQL))
    for table in tables:
        connection.execute(
            text(
                f"CREATE TRIGGER evidence_capture BEFORE INSERT ON fleetops.{table.name} "
                "FOR EACH ROW EXECUTE FUNCTION fleetops.guard_evidence_capture()"
            )
        )
    for name in ("asset_transitions", "asset_configurations"):
        connection.execute(
            text(f"""
          ALTER TABLE fleetops.{name} DROP CONSTRAINT ck_{name}_evidence_unavailable;
          ALTER TABLE fleetops.{name} ADD CONSTRAINT fk_{name}_evidence
            FOREIGN KEY (org_id,evidence_ref) REFERENCES fleetops.attachments(org_id,id);
          CREATE INDEX ix_{name}_evidence ON fleetops.{name}(org_id,evidence_ref);
          CREATE TRIGGER evidence_reference BEFORE INSERT ON fleetops.{name}
            FOR EACH ROW EXECUTE FUNCTION fleetops.guard_asset_evidence();
        """)
        )
    ordinary = replaced(
        ordinary_transition_sql(previous),
        """    IF p_evidence_ref IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE = '23514', MESSAGE = 'Evidence is unavailable';
    END IF;""",
        "    -- D17 evidence is validated by evidence_reference inside this locked INSERT.",
    )
    connection.execute(text(ordinary))
    declaration = previous.HISTORIES[0]
    old_signature = previous.signature(declaration[0], declaration[6])
    new_signature = old_signature[:-1] + ",uuid)"
    correction = previous.correction_sql(*declaration)
    correction = replaced(
        correction, "p_corrected_id uuid)", "p_corrected_id uuid,p_evidence_ref uuid DEFAULT NULL)"
    )
    correction = replaced(
        correction,
        "IF p_to_state='RETIRED' THEN RAISE EXCEPTION USING "
        "ERRCODE='23514',MESSAGE='Retirement requires evidence'; END IF;",
        "-- Evidence and disposal purpose are validated by the INSERT trigger.",
    )
    correction = replaced(
        correction,
        "correction_generation,correction_occurred_at)",
        "correction_generation,correction_occurred_at,evidence_ref)",
        2,
    )
    correction = replaced(
        correction,
        "generation,p_correction_occurred_at);",
        "generation,p_correction_occurred_at,head.evidence_ref);",
    )
    correction = replaced(
        correction,
        "p_pair_id,generation,p_correction_occurred_at) RETURNING",
        "p_pair_id,generation,p_correction_occurred_at,p_evidence_ref) RETURNING",
    )
    connection.execute(text(f"DROP FUNCTION {old_signature}"))
    connection.execute(text(correction))
    connection.execute(
        text(
            f"REVOKE ALL ON FUNCTION {new_signature} FROM "
            "PUBLIC,fleetops_app,fleetops_authenticator;"
            f"GRANT EXECUTE ON FUNCTION {new_signature} TO fleetops_app"
        )
    )
    original_anomalies = " UNION ALL ".join(
        previous.malformed_sql(h, r) for _, h, r, *_ in previous.HISTORIES
    )
    connection.execute(
        text(
            "CREATE OR REPLACE VIEW fleetops.asset_correction_anomalies "
            "WITH (security_invoker=true) AS " + original_anomalies + EVIDENCE_ANOMALIES
        )
    )
    # Pair completeness also covers copied evidence, not just physical state fields.
    connection.execute(
        text(f"""
      CREATE OR REPLACE FUNCTION fleetops.check_asset_transitions_pairs() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $pair$
      BEGIN
        IF EXISTS (SELECT 1 FROM ({
            previous.malformed_sql("asset_transitions", "corrects_transition_id")
        } {EVIDENCE_ANOMALIES}) bad
            WHERE bad.asset_id=NEW.asset_id) THEN
          RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Malformed correction history'; END IF;
        RETURN NULL;
      END $pair$;
    """)
    )


def downgrade():
    """Retained evidence cannot be silently erased to make an older schema fit."""
    connection = op.get_bind()
    previous = predecessor()
    if connection.execute(
        text("""
      SELECT EXISTS(SELECT 1 FROM fleetops.attachments)
        OR EXISTS(SELECT 1 FROM fleetops.attachment_links)
        OR EXISTS(SELECT 1 FROM fleetops.asset_transitions WHERE evidence_ref IS NOT NULL)
        OR EXISTS(SELECT 1 FROM fleetops.asset_configurations WHERE evidence_ref IS NOT NULL)
    """)
    ).scalar_one():
        raise RuntimeError("Cannot downgrade accepted evidence")
    original_anomalies = " UNION ALL ".join(
        previous.malformed_sql(h, r) for _, h, r, *_ in previous.HISTORIES
    )
    connection.execute(
        text(
            "CREATE OR REPLACE VIEW fleetops.asset_correction_anomalies "
            "WITH (security_invoker=true) AS " + original_anomalies
        )
    )
    connection.execute(
        text(f"""
          CREATE OR REPLACE FUNCTION fleetops.check_asset_transitions_pairs() RETURNS trigger
          LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $pair$
          BEGIN
            IF EXISTS (SELECT 1 FROM ({
            previous.malformed_sql("asset_transitions", "corrects_transition_id")
        }) bad
                       WHERE bad.asset_id=NEW.asset_id) THEN
              RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Malformed correction history'; END IF;
            RETURN NULL;
          END $pair$;
    """)
    )
    declaration = previous.HISTORIES[0]
    old_signature = previous.signature(declaration[0], declaration[6])
    connection.execute(text(f"DROP FUNCTION {old_signature[:-1]},uuid)"))
    connection.execute(text(previous.correction_sql(*declaration)))
    connection.execute(
        text(
            f"REVOKE ALL ON FUNCTION {old_signature} FROM "
            "PUBLIC,fleetops_app,fleetops_authenticator;"
            f"GRANT EXECUTE ON FUNCTION {old_signature} TO fleetops_app"
        )
    )
    connection.execute(text(ordinary_transition_sql(previous)))
    for name in ("asset_transitions", "asset_configurations"):
        connection.execute(
            text(f"""
          DROP TRIGGER evidence_reference ON fleetops.{name};
          DROP INDEX fleetops.ix_{name}_evidence;
          ALTER TABLE fleetops.{name} DROP CONSTRAINT fk_{name}_evidence;
          ALTER TABLE fleetops.{name} ADD CONSTRAINT ck_{name}_evidence_unavailable
            CHECK (evidence_ref IS NULL);
        """)
        )
    connection.execute(
        text("""
      DROP FUNCTION fleetops.guard_asset_evidence();
      DROP FUNCTION fleetops.valid_asset_evidence(uuid,uuid,uuid,text);
      DROP TABLE fleetops.attachment_links;
      DROP TABLE fleetops.attachments;
      DROP FUNCTION fleetops.guard_evidence_capture();
    """)
    )
