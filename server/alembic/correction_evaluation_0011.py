"""Frozen receipt evaluation integrity: source pins, predicates, and atomic consequences."""

from sqlalchemy import text


def upgrade(connection):
    """Validate Python's classifications against the immutable evaluated population."""
    connection.execute(text(SQL))


SQL = """
CREATE VIEW fleetops.evaluated_receipt_lines WITH (security_invoker=true) AS
SELECT s.org_id,s.evaluation_id,r.id AS receipt_line_id,r.receipt_id,
  CASE WHEN c.id IS NULL THEN r.po_line_id ELSE c.po_line_id END AS po_line_id,
  CASE WHEN c.id IS NULL THEN r.item_id ELSE c.item_id END AS item_id,
  CASE WHEN c.id IS NULL THEN r.quantity ELSE c.quantity END AS quantity,
  CASE WHEN c.id IS NULL THEN r.uom ELSE c.uom END AS uom,
  CASE WHEN c.id IS NULL THEN r.condition ELSE c.condition END AS condition,
  CASE WHEN c.id IS NULL THEN r.packing_quantity ELSE c.packing_quantity END AS packing_quantity,
  r.asset_id,r.serialized,
  CASE WHEN c.id IS NULL THEN i.asset_id ELSE c.conflicting_asset_id END AS conflicting_asset_id,
  EXISTS (SELECT 1 FROM fleetops.asset_identifiers u WHERE u.org_id=r.org_id
          AND u.asset_id=r.asset_id AND u.value IS NULL AND u.unreadable_reason IS NOT NULL) AS
            unreadable
FROM fleetops.receipt_evaluation_lines s
JOIN fleetops.receipt_lines r ON r.org_id=s.org_id AND r.id=s.receipt_line_id
LEFT JOIN fleetops.receipt_line_corrections c ON c.org_id=s.org_id AND c.id=s.source_id
LEFT JOIN fleetops.asset_identifiers i ON i.org_id=r.org_id
  AND i.type=r.observed_identifier_type AND i.value=r.observed_identifier_value;

CREATE VIEW fleetops.evaluated_receipt_expectations WITH (security_invoker=true) AS
SELECT s.org_id,s.evaluation_id,p.id AS po_line_id,
  CASE WHEN c.id IS NULL THEN p.item_id ELSE c.item_id END AS item_id,
  CASE WHEN c.id IS NULL THEN p.quantity ELSE c.quantity END AS quantity,
  CASE WHEN c.id IS NULL THEN p.uom ELSE c.uom END AS uom
FROM fleetops.receipt_evaluation_expectations s
JOIN fleetops.purchase_order_lines p ON p.org_id=s.org_id AND p.id=s.po_line_id
LEFT JOIN fleetops.purchase_order_line_corrections c ON c.org_id=s.org_id AND c.id=s.source_id;

CREATE VIEW fleetops.receipt_evaluation_disagreements WITH (security_invoker=true) AS
SELECT l.org_id,l.evaluation_id,l.receipt_id,l.receipt_line_id,l.po_line_id,
  CASE WHEN d.kind IN ('QUANTITY_VARIANCE','SERIAL_MISMATCH') THEN NULL ELSE l.asset_id END AS
    asset_id,
  CASE WHEN d.kind='SERIAL_MISMATCH' THEN l.conflicting_asset_id END AS conflicting_asset_id,
  d.kind AS exception_type
FROM fleetops.evaluated_receipt_lines l
LEFT JOIN fleetops.evaluated_receipt_expectations p ON p.org_id=l.org_id
  AND p.evaluation_id=l.evaluation_id AND p.po_line_id=l.po_line_id
CROSS JOIN LATERAL unnest(array_remove(ARRAY[
  CASE WHEN l.po_line_id IS NULL THEN 'UNEXPECTED_ITEM' END,
  CASE WHEN l.item_id<>p.item_id THEN 'SUBSTITUTION' END,
  CASE WHEN l.uom<>p.uom THEN 'UOM_MISMATCH' END,
  CASE WHEN l.condition IN ('DAMAGED','OPENED') THEN l.condition END,
  CASE WHEN l.unreadable THEN 'SERIAL_UNREADABLE' END,
  CASE WHEN l.conflicting_asset_id IS NOT NULL THEN 'SERIAL_MISMATCH' END,
  CASE WHEN l.packing_quantity IS NOT NULL AND l.quantity<>l.packing_quantity THEN
    'QUANTITY_VARIANCE' END
],NULL)) d(kind)
UNION ALL
SELECT p.org_id,p.evaluation_id,e.receipt_id,NULL::uuid,p.po_line_id,NULL::uuid,NULL::uuid,
  CASE WHEN COALESCE(sum(l.quantity),0)<p.quantity THEN 'SHORT' ELSE 'OVER' END
FROM fleetops.evaluated_receipt_expectations p
JOIN fleetops.receipt_correction_evaluations e ON e.org_id=p.org_id AND e.id=p.evaluation_id
LEFT JOIN fleetops.evaluated_receipt_lines l ON l.org_id=p.org_id
  AND l.evaluation_id=p.evaluation_id AND l.po_line_id=p.po_line_id
GROUP BY p.org_id,p.evaluation_id,e.receipt_id,p.po_line_id,p.quantity,p.uom
HAVING NOT COALESCE(bool_or(l.uom<>p.uom),false) AND COALESCE(sum(l.quantity),0)<>p.quantity;

CREATE FUNCTION fleetops.guard_receipt_evaluation() RETURNS trigger
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $evaluation$
DECLARE e fleetops.receipt_correction_evaluations%ROWTYPE;
  c fleetops.receipt_line_corrections%ROWTYPE;
BEGIN
  IF TG_OP<>'INSERT' THEN RAISE EXCEPTION USING
    ERRCODE='23514',MESSAGE='Evaluation history immutable'; END IF;
  IF NEW.org_id IS DISTINCT FROM NULLIF(current_setting('fleetops.org_id',true),'')::uuid
    OR NULLIF(current_setting('fleetops.actor_id',true),'') IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='Trusted credential context required'; END IF;
  NEW.actor_id:=fleetops.current_authenticated_actor();
  NEW.recorded_at:=statement_timestamp();
  IF TG_TABLE_NAME='receipt_correction_evaluations' THEN
    SELECT * INTO c FROM fleetops.receipt_line_corrections
      WHERE org_id=NEW.org_id AND id=NEW.correction_id AND correction_role='CORRECTED';
    IF NOT FOUND OR c.receipt_id<>NEW.receipt_id OR c.receipt_line_id<>NEW.receipt_line_id
      OR c.actor_id<>NEW.actor_id OR c.correction_generation<>NEW.correction_generation
      OR c.recording_transaction_id<>pg_current_xact_id()::text THEN
      RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Invalid evaluation cause'; END IF;
    PERFORM fleetops.lock_receiving_context(NEW.org_id,NEW.receipt_id);
    IF NEW.evaluation_seq<>(SELECT COALESCE(max(evaluation_seq),0)+1
      FROM fleetops.receipt_correction_evaluations WHERE org_id=NEW.org_id AND
        receipt_id=NEW.receipt_id) THEN
      RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='Evaluation sequence stale'; END IF;
    NEW.occurred_at:=c.correction_occurred_at;
    NEW.recording_transaction_id:=pg_current_xact_id()::text;
  ELSE
    SELECT * INTO e FROM fleetops.receipt_correction_evaluations
      WHERE org_id=NEW.org_id AND id=NEW.evaluation_id;
    IF NOT FOUND OR e.recording_transaction_id<>pg_current_xact_id()::text
      OR e.actor_id<>NEW.actor_id THEN
      RAISE EXCEPTION USING
        ERRCODE='23514',MESSAGE='Evaluation consequences must share transaction'; END IF;
    NEW.occurred_at:=e.occurred_at;
  END IF;
  RETURN NEW;
END $evaluation$;

CREATE FUNCTION fleetops.assert_receipt_correction_complete(p_org uuid,p_receipt uuid,p_pair uuid)
RETURNS void LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,pg_temp AS $complete$
DECLARE e fleetops.receipt_correction_evaluations%ROWTYPE;
BEGIN
  SELECT v.* INTO e FROM fleetops.receipt_correction_evaluations v
  JOIN fleetops.receipt_line_corrections c ON c.org_id=v.org_id AND c.id=v.correction_id
    WHERE c.org_id=p_org AND c.receipt_id=p_receipt AND c.correction_pair_id=p_pair
      AND c.correction_role='CORRECTED';
  IF NOT FOUND THEN RAISE EXCEPTION USING
    ERRCODE='23514',MESSAGE='Receipt correction evaluation missing'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_anomalies bad
             WHERE bad.org_id=p_org AND bad.entity_id=p_receipt)
    OR EXISTS (SELECT 1 FROM fleetops.exception_workflow_anomalies bad
               JOIN fleetops.receiving_exceptions o ON o.org_id=bad.org_id AND o.id=bad.exception_id
               WHERE o.org_id=p_org AND o.receipt_id=p_receipt) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Correction history or consequence malformed';
      END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receipt_lines r WHERE r.org_id=p_org AND r.receipt_id=p_receipt
    AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_lines s
      WHERE s.org_id=p_org AND s.evaluation_id=e.id AND s.receipt_line_id=r.id))
    OR EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_lines s
      JOIN fleetops.receipt_lines r ON r.org_id=s.org_id AND r.id=s.receipt_line_id
      WHERE s.org_id=p_org AND s.evaluation_id=e.id AND r.receipt_id<>p_receipt)
    OR EXISTS (SELECT 1 FROM fleetops.receipt_comparators b WHERE b.org_id=p_org AND
      b.receipt_id=p_receipt
      AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_expectations s
        WHERE s.org_id=p_org AND s.evaluation_id=e.id AND s.po_line_id=b.po_line_id
          AND s.source_generation=b.source_generation AND s.source_id IS NOT DISTINCT FROM
            b.source_id))
    OR EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_expectations s
      WHERE s.org_id=p_org AND s.evaluation_id=e.id AND NOT EXISTS
        (SELECT 1 FROM fleetops.receipt_comparators b WHERE b.org_id=p_org AND
          b.receipt_id=p_receipt
         AND b.po_line_id=s.po_line_id AND b.source_generation=s.source_generation
         AND b.source_id IS NOT DISTINCT FROM s.source_id))
    OR EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_lines s
      WHERE s.org_id=p_org AND s.evaluation_id=e.id AND s.source_generation<>
        (SELECT COALESCE(max(c.correction_generation),0) FROM fleetops.receipt_line_corrections c
         WHERE c.org_id=p_org AND c.receipt_line_id=s.receipt_line_id AND
           c.correction_role='CORRECTED'))
    OR EXISTS (SELECT 1 FROM fleetops.evaluated_receipt_lines l
      WHERE l.org_id=p_org AND l.evaluation_id=e.id AND l.po_line_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_expectations s
          WHERE s.org_id=p_org AND s.evaluation_id=e.id AND s.po_line_id=l.po_line_id)) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Evaluation source population incomplete'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receiving_exceptions o
      WHERE o.org_id=p_org AND o.receipt_id=p_receipt AND NOT EXISTS
        (SELECT 1 FROM fleetops.receipt_evaluation_exceptions x
         WHERE x.org_id=p_org AND x.evaluation_id=e.id AND x.exception_id=o.id)) THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Exception consequence links missing'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_exceptions x
    JOIN fleetops.receiving_exceptions o ON o.org_id=x.org_id AND o.id=x.exception_id
    WHERE x.org_id=p_org AND x.evaluation_id=e.id AND
      (o.receipt_id<>p_receipt OR x.supported IS DISTINCT FROM EXISTS
        (SELECT 1 FROM fleetops.receipt_evaluation_disagreements d WHERE d.org_id=p_org
         AND d.evaluation_id=e.id AND d.exception_type=o.exception_type
         AND d.receipt_line_id IS NOT DISTINCT FROM o.receipt_line_id
         AND d.po_line_id IS NOT DISTINCT FROM o.po_line_id
         AND d.asset_id IS NOT DISTINCT FROM o.asset_id
         AND d.conflicting_asset_id IS NOT DISTINCT FROM o.conflicting_asset_id)
       AND NOT (NOT x.supported AND EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_exceptions old
           JOIN fleetops.receipt_correction_evaluations old_e ON old_e.org_id=old.org_id AND
             old_e.id=old.evaluation_id
           WHERE old.org_id=x.org_id AND old.exception_id=x.exception_id AND
             old_e.evaluation_seq<e.evaluation_seq
           AND NOT old.supported)))) THEN
    RAISE EXCEPTION USING
      ERRCODE='23514',MESSAGE='Exception support disagrees with evaluated facts'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_disagreements d
    WHERE d.org_id=p_org AND d.evaluation_id=e.id AND 1<>(
      SELECT count(*) FROM fleetops.receipt_evaluation_exceptions x
      JOIN fleetops.receiving_exceptions o ON o.org_id=x.org_id AND o.id=x.exception_id
      WHERE x.org_id=p_org AND x.evaluation_id=e.id AND x.supported
        AND o.exception_type=d.exception_type AND o.receipt_line_id IS NOT DISTINCT FROM
          d.receipt_line_id
        AND o.po_line_id IS NOT DISTINCT FROM d.po_line_id AND o.asset_id IS NOT DISTINCT FROM
          d.asset_id
        AND o.conflicting_asset_id IS NOT DISTINCT FROM d.conflicting_asset_id)) THEN
    RAISE EXCEPTION USING
      ERRCODE='23514',MESSAGE='Disagreement needs exactly one supported observation'; END IF;
  IF EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_exceptions x
    LEFT JOIN fleetops.exception_events prior ON prior.org_id=x.org_id
      AND prior.exception_id=x.exception_id AND prior.event_seq=x.prior_event_seq
    LEFT JOIN fleetops.exception_events resolution ON resolution.org_id=x.org_id
      AND resolution.exception_id=x.exception_id AND resolution.evaluation_id=x.evaluation_id
    WHERE x.org_id=p_org AND x.evaluation_id=e.id AND (
      (x.prior_event_seq>0 AND prior.id IS NULL) OR
      (NOT x.supported AND COALESCE(prior.to_status,'OPEN') IN ('OPEN','ACKNOWLEDGED')
        AND (resolution.id IS NULL OR resolution.to_status<>'RESOLVED'
          OR resolution.event_seq<>x.prior_event_seq+1 OR resolution.actor_id<>e.actor_id
          OR resolution.occurred_at<>e.occurred_at)) OR
      ((x.supported OR prior.to_status IN ('RESOLVED','WAIVED')) AND resolution.id IS NOT NULL)))
        THEN
    RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='Correction workflow consequences incomplete';
      END IF;
END $complete$;
REVOKE ALL ON FUNCTION fleetops.guard_receipt_evaluation(),
  fleetops.assert_receipt_correction_complete(uuid,uuid,uuid) FROM
    PUBLIC,fleetops_app,fleetops_authenticator;
GRANT EXECUTE ON FUNCTION fleetops.assert_receipt_correction_complete(uuid,uuid,uuid) TO
  fleetops_app;
GRANT SELECT ON fleetops.evaluated_receipt_lines,fleetops.evaluated_receipt_expectations,
  fleetops.receipt_evaluation_disagreements TO fleetops_app;
CREATE TRIGGER evaluation_guard BEFORE INSERT OR UPDATE OR DELETE ON
  fleetops.receipt_correction_evaluations
  FOR EACH ROW EXECUTE FUNCTION fleetops.guard_receipt_evaluation();
CREATE TRIGGER evaluation_guard BEFORE INSERT OR UPDATE OR DELETE ON
  fleetops.receipt_evaluation_lines
  FOR EACH ROW EXECUTE FUNCTION fleetops.guard_receipt_evaluation();
CREATE TRIGGER evaluation_guard BEFORE INSERT OR UPDATE OR DELETE ON
  fleetops.receipt_evaluation_expectations
  FOR EACH ROW EXECUTE FUNCTION fleetops.guard_receipt_evaluation();
CREATE TRIGGER evaluation_guard BEFORE INSERT OR UPDATE OR DELETE ON
  fleetops.receipt_evaluation_exceptions
  FOR EACH ROW EXECUTE FUNCTION fleetops.guard_receipt_evaluation();
"""
