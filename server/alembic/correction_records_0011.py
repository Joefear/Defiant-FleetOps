"""Frozen Slice 10 record and Exception boundaries, used only by migration 0011."""

from sqlalchemy import MetaData, text

from fleetops.db.tenancy import apply_tenant_policy

RECORDS = (
    (
        "purchase_order_line_corrections",
        "po_line_id",
        "purchase_order_lines",
        "po_id",
        ("item_id", "quantity", "uom", "unit_price", "expected_date"),
    ),
    (
        "receipt_line_corrections",
        "receipt_line_id",
        "receipt_lines",
        "receipt_id",
        (
            "po_line_id",
            "item_id",
            "quantity",
            "uom",
            "condition",
            "packing_quantity",
            "notes",
            "serialized",
            "owner_party_id",
            "custodian_party_id",
            "asset_id",
            "observed_identifier_type",
            "observed_identifier_value",
        ),
    ),
)


def run(connection, sql):
    return connection.execute(text(sql))


def pair_anomalies(table, root, parent, entity, fields):
    """Validate typed complete generations and exact cancellation of previous authority."""
    values = ",".join("h." + field for field in fields)
    previous = ",".join(
        f"CASE WHEN h.correction_generation=1 THEN r.{field} ELSE p.{field} END" for field in fields
    )
    return f"""
      SELECT h.org_id,h.{root} AS root_id FROM fleetops.{table} h
      LEFT JOIN fleetops.{parent} r ON r.org_id=h.org_id AND r.id=h.{root}
      LEFT JOIN fleetops.{table} p ON p.org_id=h.org_id AND p.{root}=h.{root}
        AND p.correction_generation=h.correction_generation-1 AND p.correction_role='CORRECTED'
      WHERE r.id IS NULL OR h.reason !~ '[^[:space:]]'
        OR h.reason<>fleetops.normalize_correction_reason(h.reason)
        OR length(h.reason) NOT BETWEEN 1 AND 4000 OR h.{entity}<>r.{entity} OR
          (h.correction_role='REVERSAL' AND
        ((h.correction_generation>1 AND p.id IS NULL) OR
         ROW({values}) IS DISTINCT FROM ROW({previous})))
      UNION ALL
      SELECT affected.org_id,affected.{root} FROM fleetops.{table} affected JOIN (
      SELECT h.org_id,h.correction_pair_id FROM fleetops.{table} h
      GROUP BY h.org_id,h.correction_pair_id
      HAVING count(*)<>2 OR count(DISTINCT h.{root})<>1 OR count(DISTINCT h.{entity})<>1
        OR count(DISTINCT h.correction_generation)<>1
        OR count(*) FILTER (WHERE h.correction_role='REVERSAL')<>1
        OR count(*) FILTER (WHERE h.correction_role='CORRECTED')<>1
        OR count(DISTINCT h.reason)<>1 OR count(DISTINCT h.actor_id)<>1
        OR count(DISTINCT h.correction_occurred_at)<>1 OR count(DISTINCT
          h.recording_transaction_id)<>1
      ) bad ON bad.org_id=affected.org_id AND bad.correction_pair_id=affected.correction_pair_id
      UNION ALL
      SELECT h.org_id,h.{root} FROM fleetops.{table} h WHERE h.correction_role='CORRECTED'
      GROUP BY h.org_id,h.{root} HAVING count(*)<>max(h.correction_generation) OR count(DISTINCT
        h.correction_generation)<>max(h.correction_generation)
    """


def record_guard(table, root, parent, entity, fields):
    """Each invoker guard acquires its owning lock before checking authoritative generation."""
    if table == "purchase_order_line_corrections":
        admission = """
          SELECT p.* INTO owner_record FROM fleetops.purchase_orders p
            WHERE p.org_id=NEW.org_id AND p.id=original.po_id FOR NO KEY UPDATE;
          IF owner_record.status<>'ISSUED' THEN
            RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Only issued procurement is correctable'; END IF;
          IF EXISTS (SELECT 1 FROM fleetops.receipt_comparators c
            WHERE c.org_id=NEW.org_id AND c.po_line_id=original.id) THEN
            RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Receipt-bound procurement is frozen';
              END IF;
          IF NEW.po_id<>original.po_id THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Procurement lineage is immutable'; END
              IF;
        """
        owner_type = "purchase_orders"
    else:
        owner_type = "receipts"
        admission = """
          owner_record:=fleetops.lock_receiving_context(NEW.org_id,original.receipt_id);
          IF NOT EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations r
            WHERE r.org_id=NEW.org_id AND r.receipt_id=original.receipt_id) THEN
            RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Receipt correction requires sealed capture'; END IF;
          IF NEW.correction_role='REVERSAL' AND EXISTS (
            SELECT 1 FROM fleetops.receipt_evaluation_anomalies bad
            WHERE bad.org_id=NEW.org_id AND bad.entity_id=original.receipt_id) THEN
            RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Receipt consequence history malformed';
              END IF;
          IF NEW.correction_role='CORRECTED' THEN
          IF NEW.receipt_id<>original.receipt_id OR NEW.serialized<>original.serialized
            OR NEW.asset_id IS DISTINCT FROM original.asset_id
            OR NEW.owner_party_id IS DISTINCT FROM original.owner_party_id
            OR NEW.custodian_party_id IS DISTINCT FROM original.custodian_party_id THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Creation identity is immutable'; END IF;
          IF original.serialized AND (NEW.quantity<>1 OR NEW.item_id<>original.item_id) THEN
            RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Serialized creation cannot be replaced'; END IF;
          IF NOT original.serialized AND (NEW.observed_identifier_value IS NOT NULL
             OR EXISTS (SELECT 1 FROM fleetops.items i WHERE i.org_id=NEW.org_id
                        AND i.id=NEW.item_id AND i.serialized)) THEN
            RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Nonserialized capture cannot create an Asset'; END IF;
          IF original.asset_id IS NOT NULL AND
            (NEW.observed_identifier_type IS NOT NULL OR NEW.observed_identifier_value IS NOT
              NULL) THEN
            RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Normal creation cannot become a conflict'; END IF;
          IF NEW.packing_quantity IS NOT NULL AND owner_record.packing_reference IS NULL THEN
            RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Packing count requires reference'; END
              IF;
          -- ADR-013: retain the locked effective comparator even after supersession.
          -- Only a different selection needs active-leaf admission. Do not fall back
          -- from an effective NULL comparator to the original line's old selection.
          IF NEW.po_line_id IS NOT NULL AND NEW.po_line_id IS DISTINCT FROM (
            SELECT h.po_line_id FROM fleetops.effective_receipt_lines h
            WHERE h.org_id=NEW.org_id AND h.id=original.id
          ) AND NOT EXISTS (
            SELECT 1 FROM fleetops.purchase_order_lines p
            JOIN fleetops.purchase_orders po ON po.org_id=p.org_id AND po.id=p.po_id
            WHERE p.org_id=NEW.org_id AND p.id=NEW.po_line_id AND p.po_id=owner_record.po_id
              AND po.status='ISSUED' AND NOT EXISTS (SELECT 1 FROM fleetops.purchase_order_lines s
                WHERE s.org_id=p.org_id AND s.supersedes_line_id=p.id)) THEN
            RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Invalid active receipt comparator'; END
              IF;
          END IF;
          NEW.conflicting_asset_id:=NULL;
          IF original.observed_identifier_value IS NOT NULL THEN
            SELECT i.asset_id INTO NEW.conflicting_asset_id FROM fleetops.asset_identifiers i
              WHERE i.org_id=NEW.org_id AND i.type=NEW.observed_identifier_type
              AND i.value=NEW.observed_identifier_value;
            IF NOT FOUND THEN RAISE EXCEPTION USING
              ERRCODE='23514',MESSAGE='Canonical conflict required'; END IF;
          END IF;
        """
    domain_time = "created_at" if table == "purchase_order_line_corrections" else "occurred_at"
    consequence = (
        "PERFORM fleetops.assert_receipt_correction_complete("
        "NEW.org_id,NEW.receipt_id,NEW.correction_pair_id);"
        if table == "receipt_line_corrections"
        else ""
    )
    return f"""
      CREATE FUNCTION fleetops.guard_{table}() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $record$
      DECLARE original fleetops.{parent}%ROWTYPE; owner_record fleetops.{owner_type}%ROWTYPE;
        generation integer;
      BEGIN
        IF TG_OP<>'INSERT' THEN RAISE EXCEPTION USING
          ERRCODE='23514',MESSAGE='Correction history immutable'; END IF;
        IF NEW.org_id IS DISTINCT FROM NULLIF(current_setting('fleetops.org_id',true),'')::uuid
          OR NULLIF(current_setting('fleetops.actor_id',true),'') IS NOT NULL THEN
          RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Trusted credential context required'; END
            IF;
        PERFORM fleetops.current_authenticated_actor();
        IF current_setting('transaction_isolation')<>'read committed' THEN
          RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Correction requires READ COMMITTED'; END
            IF;
        SELECT p.* INTO original FROM fleetops.{parent} p
          WHERE p.org_id=NEW.org_id AND p.id=NEW.{root};
        IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='23503',MESSAGE='Ordinary root not found';
          END IF;
        {admission}
        SELECT COALESCE(max(c.correction_generation),0) INTO generation FROM fleetops.{table} c
          WHERE c.org_id=NEW.org_id AND c.{root}=NEW.{root} AND c.correction_role='CORRECTED';
        IF NEW.correction_generation<>generation+1 THEN
          RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Correction generation stale'; END IF;
        IF NEW.correction_role='REVERSAL' AND EXISTS
          (SELECT 1 FROM fleetops.{table}_anomalies b WHERE b.org_id=NEW.org_id AND
            b.root_id=NEW.{root}) THEN
          RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Malformed correction authority'; END IF;
        NEW.actor_id:=fleetops.current_authenticated_actor();
        NEW.recorded_at:=statement_timestamp();
        NEW.recording_transaction_id:=pg_current_xact_id()::text;
        NEW.occurred_at:=original.{domain_time};
        NEW.reason:=fleetops.normalize_correction_reason(NEW.reason);
        RETURN NEW;
      END $record$;
      CREATE FUNCTION fleetops.complete_{table}() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $complete$
      BEGIN
        IF EXISTS (SELECT 1 FROM fleetops.{table}_anomalies b
                   WHERE b.org_id=NEW.org_id AND b.root_id=NEW.{root}) THEN
          RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Incomplete correction pair'; END IF;
        {consequence}
        RETURN NULL;
      END $complete$;
    """


WORKFLOW_SQL = """
CREATE FUNCTION fleetops.initialize_exception_workflow() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $origin$
BEGIN
  IF NEW.org_id IS DISTINCT FROM NULLIF(current_setting('fleetops.org_id',true),'')::uuid THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Tenant context required'; END IF;
  PERFORM fleetops.current_authenticated_actor();
  -- Primary context is derived from immutable typed receiving references, never caller input.
  INSERT INTO fleetops.exception_workflows(
    exception_id,org_id,exception_type,entity_type,entity_id)
  VALUES (NEW.id,NEW.org_id,NEW.exception_type,
    CASE WHEN NEW.receipt_line_id IS NULL THEN 'RECEIPT' ELSE 'RECEIPT_LINE' END,
    COALESCE(NEW.receipt_line_id,NEW.receipt_id));
  RETURN NULL;
END $origin$;

CREATE VIEW fleetops.exception_workflow_anomalies WITH (security_invoker=true) AS
SELECT o.org_id,o.id AS exception_id FROM fleetops.receiving_exceptions o
LEFT JOIN fleetops.exception_workflows w ON w.org_id=o.org_id AND w.exception_id=o.id
LEFT JOIN LATERAL (SELECT * FROM fleetops.exception_events e
  WHERE e.org_id=o.org_id AND e.exception_id=o.id ORDER BY event_seq DESC LIMIT 1) e ON true
WHERE w.exception_id IS NULL OR w.exception_type IS DISTINCT FROM o.exception_type
  OR w.severity IS DISTINCT FROM 'UNSPECIFIED'
  OR w.entity_type IS DISTINCT FROM
    CASE WHEN o.receipt_line_id IS NULL THEN 'RECEIPT' ELSE 'RECEIPT_LINE' END
  OR w.entity_id IS DISTINCT FROM COALESCE(o.receipt_line_id,o.receipt_id)
  OR w.status IS DISTINCT FROM COALESCE(e.to_status,'OPEN')
  OR w.event_seq IS DISTINCT FROM COALESCE(e.event_seq,0)
  OR w.event_seq<>(SELECT count(*) FROM fleetops.exception_events n
                  WHERE n.org_id=o.org_id AND n.exception_id=o.id)
  OR w.resolved_by_actor_id IS DISTINCT FROM CASE WHEN e.to_status IN ('RESOLVED','WAIVED') THEN
    e.actor_id END
  OR w.resolved_at IS DISTINCT FROM CASE WHEN e.to_status IN ('RESOLVED','WAIVED') THEN
    e.occurred_at END
  OR w.resolution_note IS DISTINCT FROM CASE WHEN e.to_status IN ('RESOLVED','WAIVED') THEN e.note
    END
UNION ALL SELECT e.org_id,e.exception_id FROM fleetops.exception_events e
LEFT JOIN fleetops.exception_events p ON p.org_id=e.org_id AND p.exception_id=e.exception_id
  AND p.event_seq=e.event_seq-1
WHERE e.from_status IS DISTINCT FROM COALESCE(p.to_status,'OPEN')
  OR (e.event_seq>1 AND p.id IS NULL)
  OR NOT ((e.from_status='OPEN' AND e.to_status IN ('ACKNOWLEDGED','RESOLVED','WAIVED'))
          OR (e.from_status='ACKNOWLEDGED' AND e.to_status IN ('RESOLVED','WAIVED')));

CREATE FUNCTION fleetops.transition_exception(p_exception_id uuid,p_expected_status text,
  p_to_status text,p_note text,p_occurred_at timestamptz,p_event_id uuid,p_evaluation_id uuid)
RETURNS fleetops.exception_events LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path=pg_catalog,pg_temp AS $workflow$
DECLARE trusted_org uuid; performer uuid; w fleetops.exception_workflows%ROWTYPE;
  latest fleetops.exception_events%ROWTYPE; produced fleetops.exception_events%ROWTYPE;
  evaluation fleetops.receipt_correction_evaluations%ROWTYPE;
BEGIN
  trusted_org:=NULLIF(current_setting('fleetops.org_id',true),'')::uuid;
  IF trusted_org IS NULL OR NULLIF(current_setting('fleetops.actor_id',true),'') IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Trusted credential context required'; END IF;
  performer:=fleetops.current_authenticated_actor();
  SELECT * INTO w FROM fleetops.exception_workflows
    WHERE org_id=trusted_org AND exception_id=p_exception_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='P0002',MESSAGE='Exception not found'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.exception_workflow_anomalies bad
             WHERE bad.org_id=trusted_org AND bad.exception_id=p_exception_id) THEN
    RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Workflow projection disagrees with history';
      END IF;
  SELECT * INTO latest FROM fleetops.exception_events
    WHERE org_id=trusted_org AND exception_id=p_exception_id ORDER BY event_seq DESC LIMIT 1;
  IF p_expected_status IS DISTINCT FROM COALESCE(latest.to_status,'OPEN') THEN
    RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Exception status stale'; END IF;
  IF p_evaluation_id IS NOT NULL THEN
    SELECT * INTO evaluation FROM fleetops.receipt_correction_evaluations
      WHERE org_id=trusted_org AND id=p_evaluation_id;
    IF NOT FOUND OR evaluation.actor_id<>performer OR evaluation.occurred_at<>p_occurred_at
      OR evaluation.recording_transaction_id<>pg_current_xact_id()::text OR
        p_to_status<>'RESOLVED' OR NOT EXISTS (SELECT 1 FROM
          fleetops.receipt_evaluation_exceptions x
        WHERE x.org_id=trusted_org AND x.evaluation_id=p_evaluation_id
        AND x.exception_id=p_exception_id AND NOT x.supported AND x.prior_event_seq=w.event_seq)
          THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid correction-derived transition'; END IF;
  END IF;
  INSERT INTO fleetops.exception_events
    (id,org_id,exception_id,event_seq,from_status,to_status,note,actor_id,occurred_at,evaluation_id)
  VALUES (p_event_id,trusted_org,p_exception_id,COALESCE(latest.event_seq,0)+1,
    COALESCE(latest.to_status,'OPEN'),p_to_status,
    fleetops.normalize_correction_reason(p_note),performer,p_occurred_at,p_evaluation_id)
  RETURNING * INTO produced;
  UPDATE fleetops.exception_workflows SET status=produced.to_status,event_seq=produced.event_seq,
    resolved_by_actor_id=CASE WHEN produced.to_status IN ('RESOLVED','WAIVED') THEN performer END,
    resolved_at=CASE WHEN produced.to_status IN ('RESOLVED','WAIVED') THEN produced.occurred_at END,
    resolution_note=CASE WHEN produced.to_status IN ('RESOLVED','WAIVED') THEN produced.note END
  WHERE org_id=trusted_org AND exception_id=p_exception_id;
  RETURN produced;
END $workflow$;
"""


def upgrade(connection, schema_module, predecessor):
    """Create typed histories and minimal privileges; expose no mutable observation API."""
    metadata = MetaData(schema="fleetops")
    metadata.reflect(bind=connection)
    tables = schema_module.define_record_corrections(metadata)
    for table in tables:
        table.create(connection)
        apply_tenant_policy(connection, table, privileges=("SELECT",))
        if table.name not in ("exception_workflows", "exception_events"):
            columns = [
                c.name
                for c in table.c
                if c.name
                not in (
                    "actor_id",
                    "recorded_at",
                    "source_role",
                    "conflicting_asset_id",
                    "recording_transaction_id",
                )
            ]
            run(
                connection,
                f"GRANT INSERT ({','.join(columns)}) ON fleetops.{table.name} TO fleetops_app",
            )
        if table.name != "exception_workflows":
            run(
                connection,
                f"CREATE TRIGGER attributed_creator AFTER INSERT ON fleetops.{table.name} "
                "FOR EACH ROW EXECUTE FUNCTION fleetops.enforce_authenticated_creator('actor_id')",
            )
    run(
        connection,
        """
      ALTER TABLE fleetops.receipt_comparators
        ADD COLUMN source_generation integer NOT NULL DEFAULT 0,ADD COLUMN source_id uuid,
        ADD COLUMN introduced_by_correction_id uuid,
        ADD COLUMN source_role text GENERATED ALWAYS AS ('CORRECTED'::text) STORED NOT NULL,
        ADD CONSTRAINT ck_receipt_comparators_source CHECK (
          (source_generation=0 AND source_id IS NULL) OR (source_generation>0 AND source_id IS NOT
            NULL)),
        ADD CONSTRAINT fk_receipt_comparators_source FOREIGN KEY
          (org_id,po_line_id,source_generation,source_id,source_role)
          REFERENCES fleetops.purchase_order_line_corrections
          (org_id,po_line_id,correction_generation,id,correction_role);
      ALTER TABLE fleetops.receipt_comparators ADD CONSTRAINT fk_receipt_comparators_introduction
        FOREIGN KEY (org_id,receipt_id,introduced_by_correction_id,source_role)
        REFERENCES fleetops.receipt_line_corrections(org_id,receipt_id,id,correction_role)
          DEFERRABLE INITIALLY DEFERRED;
      GRANT INSERT (source_generation,source_id,introduced_by_correction_id) ON
        fleetops.receipt_comparators TO fleetops_app;
      ALTER TABLE fleetops.receiving_exceptions ADD COLUMN evaluation_id uuid,
        ADD CONSTRAINT fk_receiving_exceptions_evaluation FOREIGN KEY (org_id,evaluation_id)
          REFERENCES fleetops.receipt_correction_evaluations(org_id,id);
      GRANT INSERT (evaluation_id) ON fleetops.receiving_exceptions TO fleetops_app;
      DROP INDEX fleetops.uq_receiving_exceptions_line;
      DROP INDEX fleetops.uq_receiving_exceptions_aggregate;
      CREATE UNIQUE INDEX uq_receiving_exceptions_line ON fleetops.receiving_exceptions
        (org_id,receipt_line_id,exception_type) WHERE receipt_line_id IS NOT NULL AND
          evaluation_id IS NULL;
      CREATE UNIQUE INDEX uq_receiving_exceptions_aggregate ON fleetops.receiving_exceptions
        (org_id,receipt_id,po_line_id,exception_type) WHERE receipt_line_id IS NULL AND
          evaluation_id IS NULL;
      CREATE UNIQUE INDEX uq_receiving_exceptions_evaluation_line ON fleetops.receiving_exceptions
        (org_id,evaluation_id,receipt_line_id,exception_type) WHERE receipt_line_id IS NOT NULL
          AND evaluation_id IS NOT NULL;
      CREATE UNIQUE INDEX uq_receiving_exceptions_evaluation_aggregate ON
        fleetops.receiving_exceptions
        (org_id,evaluation_id,po_line_id,exception_type) WHERE receipt_line_id IS NULL AND
          evaluation_id IS NOT NULL;
      -- Existing observations are their own opening authority, with no fabricated events
      -- or severity assessment. Their original rows and timestamps remain unchanged.
      INSERT INTO fleetops.exception_workflows(
        exception_id,org_id,exception_type,entity_type,entity_id)
        SELECT id,org_id,exception_type,
          CASE WHEN receipt_line_id IS NULL THEN 'RECEIPT' ELSE 'RECEIPT_LINE' END,
          COALESCE(receipt_line_id,receipt_id) FROM fleetops.receiving_exceptions;
    """,
    )
    for declaration in RECORDS:
        table, root, parent, entity, fields = declaration
        run(
            connection,
            f"CREATE VIEW fleetops.{table}_anomalies WITH (security_invoker=true) "
            f"AS {pair_anomalies(*declaration)}",
        )
        run(connection, f"GRANT SELECT ON fleetops.{table}_anomalies TO fleetops_app")
        run(connection, record_guard(*declaration))
        run(
            connection,
            f"""
          CREATE TRIGGER correction_record_guard BEFORE INSERT OR UPDATE OR DELETE ON
            fleetops.{table}
            FOR EACH ROW EXECUTE FUNCTION fleetops.guard_{table}();
          CREATE CONSTRAINT TRIGGER correction_record_complete AFTER INSERT ON fleetops.{table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fleetops.complete_{table}();
          REVOKE ALL ON FUNCTION fleetops.guard_{table}(),fleetops.complete_{table}()
            FROM PUBLIC,fleetops_app,fleetops_authenticator;
        """,
        )
        # Preserve the ordinary record's public identity and column shape. Source
        # IDs and generations are exposed separately, not substituted for root IDs.
        projections = ",".join(
            f"CASE WHEN c.id IS NULL THEN p.{column.name} ELSE c.{column.name} END AS {column.name}"
            if column.name in fields
            else f"p.{column.name}"
            for column in metadata.tables[f"fleetops.{parent}"].c
        )
        run(
            connection,
            f"""
          CREATE VIEW fleetops.effective_{parent} WITH (security_invoker=true) AS
          SELECT {projections} FROM fleetops.{parent} p LEFT JOIN LATERAL
            (SELECT * FROM fleetops.{table} c WHERE c.org_id=p.org_id AND c.{root}=p.id
             AND c.correction_role='CORRECTED' ORDER BY c.correction_generation DESC LIMIT 1) c ON
               true;
          GRANT SELECT ON fleetops.effective_{parent} TO fleetops_app;
        """,
        )
    run(connection, WORKFLOW_SQL)
    run(
        connection,
        """
      GRANT SELECT ON fleetops.exception_workflow_anomalies TO fleetops_app;
      -- A column grant is needed for invoker SELECT FOR UPDATE. The corresponding
      -- trigger rejects even identity-preserving UPDATE; it is lock permission only.
      GRANT UPDATE (exception_id) ON fleetops.exception_workflows TO fleetops_app;
      CREATE FUNCTION fleetops.guard_exception_workflow_identity() RETURNS trigger
      LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $identity$
      BEGIN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Workflow identity is immutable';
      END $identity$;
      REVOKE ALL ON FUNCTION fleetops.guard_exception_workflow_identity()
        FROM PUBLIC,fleetops_app,fleetops_authenticator;
      CREATE TRIGGER workflow_identity_immutable BEFORE UPDATE OF
        exception_id,org_id,exception_type,severity,entity_type,entity_id ON
        fleetops.exception_workflows
        FOR EACH ROW EXECUTE FUNCTION fleetops.guard_exception_workflow_identity();
      CREATE TRIGGER exception_workflow_origin AFTER INSERT ON fleetops.receiving_exceptions
        FOR EACH ROW EXECUTE FUNCTION fleetops.initialize_exception_workflow();
      REVOKE ALL ON FUNCTION fleetops.initialize_exception_workflow(),
        fleetops.transition_exception(uuid,text,text,text,timestamptz,uuid,uuid)
        FROM PUBLIC,fleetops_app,fleetops_authenticator;
      GRANT EXECUTE ON FUNCTION
        fleetops.transition_exception(uuid,text,text,text,timestamptz,uuid,uuid)
        TO fleetops_app;
    """,
    )


def receiving_sql(predecessor):
    """Extend only admission and comparison reads; preserve the normal creation bundle."""
    sql = predecessor.GUARDS.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION")
    sql = sql.replace(
        "FROM fleetops.purchase_order_lines p", "FROM fleetops.effective_purchase_order_lines p"
    )
    # D48: these observations follow an already-locked receipt evaluation, not
    # another physical arrival. Only a same-org, same-receipt evaluation by
    # this authenticated Actor in this transaction may supply occurrence and
    # bypass the ordinary receiving-context/occurrence path.
    sql = sql.replace(
        "    NEW.recorded_at := pg_catalog.statement_timestamp();",
        """
    NEW.recorded_at := pg_catalog.statement_timestamp();
    IF TG_TABLE_NAME='receiving_exceptions' THEN
      IF NEW.evaluation_id IS NOT NULL THEN
        SELECT e.occurred_at INTO NEW.occurred_at FROM fleetops.receipt_correction_evaluations e
          WHERE e.org_id=NEW.org_id AND e.id=NEW.evaluation_id AND e.receipt_id=NEW.receipt_id
            AND e.actor_id=fleetops.current_authenticated_actor()
            AND e.recording_transaction_id=pg_current_xact_id()::text;
        IF NOT FOUND THEN RAISE EXCEPTION USING
          ERRCODE='23514',MESSAGE='Invalid correction observation cause'; END IF;
        RETURN NEW;
      END IF;
    END IF;
    """,
    )
    # D48 permits a newly selected comparator as typed correction context
    # after sealing, without admitting physical lines or editing old context.
    # The exception requires this exact org/receipt/source introduction by a
    # CORRECTED member in the current transaction; record_guard binds that
    # member to the authenticated Actor. The next branch validates the typed
    # introduction and source generation as well.
    sql = sql.replace(
        "AND EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations f",
        """AND NOT (TG_TABLE_NAME='receipt_comparators' AND EXISTS (
         SELECT 1 FROM fleetops.receipt_line_corrections c
         WHERE c.org_id=NEW.org_id AND c.receipt_id=NEW.receipt_id AND
           c.po_line_id=(to_jsonb(NEW)->>'po_line_id')::uuid
           AND c.id=(to_jsonb(NEW)->>'introduced_by_correction_id')::uuid
           AND c.correction_role='CORRECTED' AND
             c.recording_transaction_id=pg_current_xact_id()::text))
       AND EXISTS (SELECT 1 FROM fleetops.receipt_reconciliations f""",
        1,
    )
    sql = sql.replace(
        "    IF TG_TABLE_NAME = 'receipt_lines' THEN",
        """
    IF TG_TABLE_NAME='receipt_comparators' THEN
      IF NEW.introduced_by_correction_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM fleetops.receipt_line_corrections c WHERE c.org_id=NEW.org_id
          AND c.id=NEW.introduced_by_correction_id AND c.receipt_id=NEW.receipt_id
          AND c.po_line_id=NEW.po_line_id AND c.correction_role='CORRECTED'
          AND c.recording_transaction_id=pg_current_xact_id()::text) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid comparator introduction'; END IF;
      IF NEW.source_generation<>(SELECT COALESCE(max(c.correction_generation),0)
          FROM fleetops.purchase_order_line_corrections c WHERE c.org_id=NEW.org_id
            AND c.po_line_id=NEW.po_line_id AND c.correction_role='CORRECTED') THEN
        RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Expected procurement generation stale'; END
          IF;
      IF EXISTS (SELECT 1 FROM fleetops.purchase_order_line_corrections_anomalies bad
                 WHERE bad.org_id=NEW.org_id AND bad.root_id=NEW.po_line_id) THEN
        RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Malformed procurement authority'; END IF;
    END IF;
    IF TG_TABLE_NAME = 'receipt_lines' THEN""",
        1,
    )
    sql = sql.replace(
        "    IF NEW.receipt_line_id IS NULL THEN",
        """
    IF NEW.evaluation_id IS NOT NULL THEN
      IF NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_disagreements d
        WHERE d.org_id=NEW.org_id AND d.evaluation_id=NEW.evaluation_id AND
          d.receipt_id=NEW.receipt_id
          AND d.exception_type=NEW.exception_type AND d.receipt_line_id IS NOT DISTINCT FROM
            NEW.receipt_line_id
          AND d.po_line_id IS NOT DISTINCT FROM NEW.po_line_id AND d.asset_id IS NOT DISTINCT FROM
            NEW.asset_id
          AND d.conflicting_asset_id IS NOT DISTINCT FROM NEW.conflicting_asset_id) THEN
        RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Correction observation unsupported'; END IF;
      RETURN NEW;
    END IF;
    IF NEW.receipt_line_id IS NULL THEN""",
        1,
    )
    return sql


def downgrade(connection, predecessor):
    """Restore 0010 exactly when no accepted correction or workflow event would be lost."""
    for table in (
        "purchase_order_line_corrections",
        "receipt_line_corrections",
        "exception_events",
    ):
        if run(connection, f"SELECT EXISTS (SELECT 1 FROM fleetops.{table})").scalar_one():
            raise RuntimeError("Cannot discard accepted correction or Exception history")
    run(connection, predecessor.GUARDS.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION"))
    run(connection, "DROP TRIGGER exception_workflow_origin ON fleetops.receiving_exceptions")
    for table in (
        "receipt_correction_evaluations",
        "receipt_evaluation_lines",
        "receipt_evaluation_expectations",
        "receipt_evaluation_exceptions",
    ):
        run(connection, f"DROP TRIGGER evaluation_guard ON fleetops.{table}")
    for table, *_ in RECORDS:
        run(connection, f"DROP TRIGGER correction_record_guard ON fleetops.{table}")
        run(connection, f"DROP TRIGGER correction_record_complete ON fleetops.{table}")
    run(connection, "DROP TRIGGER workflow_identity_immutable ON fleetops.exception_workflows")
    for name in (
        "guard_exception_workflow_identity()",
        "transition_exception(uuid,text,text,text,timestamptz,uuid,uuid)",
        "initialize_exception_workflow()",
        "guard_receipt_evaluation()",
        "assert_receipt_correction_complete(uuid,uuid,uuid)",
        "guard_purchase_order_line_corrections()",
        "complete_purchase_order_line_corrections()",
        "guard_receipt_line_corrections()",
        "complete_receipt_line_corrections()",
    ):
        run(connection, f"DROP FUNCTION fleetops.{name}")
    for name in (
        "correction_anomalies",
        "receipt_evaluation_anomalies",
        "receipt_evaluation_disagreements",
        "evaluated_receipt_lines",
        "evaluated_receipt_expectations",
        "exception_workflow_anomalies",
        "effective_receipt_lines",
        "effective_purchase_order_lines",
        "receipt_line_corrections_anomalies",
        "purchase_order_line_corrections_anomalies",
    ):
        run(connection, f"DROP VIEW fleetops.{name}")
    run(
        connection,
        """
      DROP INDEX fleetops.uq_receiving_exceptions_line;
      DROP INDEX fleetops.uq_receiving_exceptions_aggregate;
      ALTER TABLE fleetops.receiving_exceptions DROP COLUMN evaluation_id;
      CREATE UNIQUE INDEX uq_receiving_exceptions_line ON fleetops.receiving_exceptions
        (org_id,receipt_line_id,exception_type) WHERE receipt_line_id IS NOT NULL;
      CREATE UNIQUE INDEX uq_receiving_exceptions_aggregate ON fleetops.receiving_exceptions
        (org_id,receipt_id,po_line_id,exception_type) WHERE receipt_line_id IS NULL;
      ALTER TABLE fleetops.receipt_comparators DROP COLUMN source_generation,
        DROP COLUMN source_id,DROP COLUMN source_role,DROP COLUMN introduced_by_correction_id;
    """,
    )
    for table in (
        "receipt_evaluation_exceptions",
        "exception_events",
        "exception_workflows",
        "receipt_evaluation_expectations",
        "receipt_evaluation_lines",
        "receipt_correction_evaluations",
        "receipt_line_corrections",
        "purchase_order_line_corrections",
    ):
        run(connection, f"DROP TABLE fleetops.{table}")
