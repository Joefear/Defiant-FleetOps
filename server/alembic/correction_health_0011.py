"""Frozen correction health views reconstruct historical evaluations without live retargeting."""

from sqlalchemy import text


def upgrade(connection):
    """Report authority discrepancies; no health path writes or repairs history."""
    connection.execute(text(SQL))


SQL = """
CREATE VIEW fleetops.receipt_evaluation_anomalies WITH (security_invoker=true) AS
SELECT c.org_id,c.receipt_id AS entity_id,'missing_correction_evaluation'::text AS issue
FROM fleetops.receipt_line_corrections c
LEFT JOIN fleetops.receipt_correction_evaluations e ON e.org_id=c.org_id AND e.correction_id=c.id
WHERE c.correction_role='CORRECTED' AND (e.id IS NULL OR e.receipt_id<>c.receipt_id
  OR e.receipt_line_id<>c.receipt_line_id OR e.correction_generation<>c.correction_generation
  OR e.actor_id<>c.actor_id OR e.occurred_at<>c.correction_occurred_at
  OR e.recording_transaction_id<>c.recording_transaction_id)
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_line_population' FROM
  fleetops.receipt_correction_evaluations e
WHERE EXISTS (SELECT 1 FROM fleetops.receipt_lines r WHERE r.org_id=e.org_id AND
  r.receipt_id=e.receipt_id
  AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_lines s WHERE s.org_id=e.org_id
    AND s.evaluation_id=e.id AND s.receipt_line_id=r.id))
 OR EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_lines s
   JOIN fleetops.receipt_lines r ON r.org_id=s.org_id AND r.id=s.receipt_line_id
   WHERE s.org_id=e.org_id AND s.evaluation_id=e.id AND (r.receipt_id<>e.receipt_id
     OR s.actor_id<>e.actor_id OR s.occurred_at<>e.occurred_at
     OR s.source_generation<>(SELECT COALESCE(max(c.correction_generation),0)
       FROM fleetops.receipt_line_corrections c
       JOIN fleetops.receipt_correction_evaluations cause ON cause.org_id=c.org_id AND
         cause.correction_id=c.id
       WHERE c.org_id=e.org_id AND c.receipt_line_id=s.receipt_line_id
         AND cause.receipt_id=e.receipt_id AND cause.evaluation_seq<=e.evaluation_seq)))
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_expectation_population' FROM
  fleetops.receipt_correction_evaluations e
WHERE EXISTS (SELECT 1 FROM fleetops.receipt_comparators b
  LEFT JOIN fleetops.receipt_correction_evaluations intro ON intro.org_id=b.org_id
    AND intro.correction_id=b.introduced_by_correction_id
  WHERE b.org_id=e.org_id AND b.receipt_id=e.receipt_id
    AND (b.introduced_by_correction_id IS NULL OR intro.evaluation_seq<=e.evaluation_seq)
    AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_expectations s
      WHERE s.org_id=e.org_id AND s.evaluation_id=e.id AND s.po_line_id=b.po_line_id
        AND s.source_generation=b.source_generation AND s.source_id IS NOT DISTINCT FROM
          b.source_id))
 OR EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_expectations s
   WHERE s.org_id=e.org_id AND s.evaluation_id=e.id AND (s.actor_id<>e.actor_id
     OR s.occurred_at<>e.occurred_at OR NOT EXISTS (
       SELECT 1 FROM fleetops.receipt_comparators b
       LEFT JOIN fleetops.receipt_correction_evaluations intro ON intro.org_id=b.org_id
         AND intro.correction_id=b.introduced_by_correction_id
       WHERE b.org_id=e.org_id AND b.receipt_id=e.receipt_id AND b.po_line_id=s.po_line_id
         AND b.source_generation=s.source_generation AND b.source_id IS NOT DISTINCT FROM
           s.source_id
         AND (b.introduced_by_correction_id IS NULL OR intro.evaluation_seq<=e.evaluation_seq))))
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_exception_links' FROM
  fleetops.receipt_correction_evaluations e
WHERE EXISTS (SELECT 1 FROM fleetops.receiving_exceptions o
  LEFT JOIN fleetops.receipt_correction_evaluations origin ON origin.org_id=o.org_id AND
    origin.id=o.evaluation_id
  WHERE o.org_id=e.org_id AND o.receipt_id=e.receipt_id
    AND (o.evaluation_id IS NULL OR origin.evaluation_seq<=e.evaluation_seq)
    AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_exceptions x
      WHERE x.org_id=e.org_id AND x.evaluation_id=e.id AND x.exception_id=o.id))
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_exception_support' FROM
  fleetops.receipt_correction_evaluations e
JOIN fleetops.receipt_evaluation_exceptions x ON x.org_id=e.org_id AND x.evaluation_id=e.id
JOIN fleetops.receiving_exceptions o ON o.org_id=x.org_id AND o.id=x.exception_id
WHERE o.receipt_id<>e.receipt_id OR x.actor_id<>e.actor_id OR x.occurred_at<>e.occurred_at
 OR x.supported IS DISTINCT FROM (
   EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_disagreements d WHERE d.org_id=e.org_id
     AND d.evaluation_id=e.id AND d.exception_type=o.exception_type
     AND d.receipt_line_id IS NOT DISTINCT FROM o.receipt_line_id
     AND d.po_line_id IS NOT DISTINCT FROM o.po_line_id AND d.asset_id IS NOT DISTINCT FROM
       o.asset_id
     AND d.conflicting_asset_id IS NOT DISTINCT FROM o.conflicting_asset_id)
   AND NOT EXISTS (SELECT 1 FROM fleetops.receipt_evaluation_exceptions old
     JOIN fleetops.receipt_correction_evaluations old_e ON old_e.org_id=old.org_id AND
       old_e.id=old.evaluation_id
     WHERE old.org_id=e.org_id AND old.exception_id=o.id AND NOT old.supported
       AND old_e.receipt_id=e.receipt_id AND old_e.evaluation_seq<e.evaluation_seq))
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_disagreement_coverage' FROM
  fleetops.receipt_correction_evaluations e
JOIN fleetops.receipt_evaluation_disagreements d ON d.org_id=e.org_id AND d.evaluation_id=e.id
WHERE 1<>(SELECT count(*) FROM fleetops.receipt_evaluation_exceptions x
  JOIN fleetops.receiving_exceptions o ON o.org_id=x.org_id AND o.id=x.exception_id
  WHERE x.org_id=e.org_id AND x.evaluation_id=e.id AND x.supported
    AND o.exception_type=d.exception_type AND o.receipt_line_id IS NOT DISTINCT FROM
      d.receipt_line_id
    AND o.po_line_id IS NOT DISTINCT FROM d.po_line_id AND o.asset_id IS NOT DISTINCT FROM
      d.asset_id
    AND o.conflicting_asset_id IS NOT DISTINCT FROM d.conflicting_asset_id)
UNION ALL
SELECT e.org_id,e.receipt_id,'evaluation_workflow_consequence' FROM
  fleetops.receipt_correction_evaluations e
JOIN fleetops.receipt_line_corrections cause ON cause.org_id=e.org_id AND cause.id=e.correction_id
JOIN fleetops.receipt_evaluation_exceptions x ON x.org_id=e.org_id AND x.evaluation_id=e.id
LEFT JOIN fleetops.exception_events prior ON prior.org_id=x.org_id
  AND prior.exception_id=x.exception_id AND prior.event_seq=x.prior_event_seq
LEFT JOIN fleetops.exception_events resolution ON resolution.org_id=x.org_id
  AND resolution.exception_id=x.exception_id AND resolution.evaluation_id=x.evaluation_id
WHERE (x.prior_event_seq>0 AND prior.id IS NULL)
 OR (NOT x.supported AND COALESCE(prior.to_status,'OPEN') IN ('OPEN','ACKNOWLEDGED')
   AND (resolution.id IS NULL OR resolution.to_status<>'RESOLVED'
     OR resolution.event_seq<>x.prior_event_seq+1 OR resolution.actor_id<>e.actor_id
     OR resolution.occurred_at<>e.occurred_at
     -- D11/D42: the note explains supersession and references the exact pair's
     -- unchanged reason, including reasons already at the 4,000-character limit.
     OR resolution.note IS DISTINCT FROM (
       'Corrected receipt reality superseded this observation''s assertion. '
       || 'Reason: see receipt-line correction pair ' || cause.correction_pair_id::text || '.')))
 OR ((x.supported OR prior.to_status IN ('RESOLVED','WAIVED')) AND resolution.id IS NOT NULL);

CREATE VIEW fleetops.correction_anomalies WITH (security_invoker=true) AS
SELECT a.org_id,'ASSET'::text AS entity_type,a.id AS entity_id,'malformed_asset_pair'::text AS issue
FROM fleetops.assets a JOIN fleetops.asset_correction_anomalies bad ON bad.asset_id=a.id
UNION ALL SELECT org_id,'PO_LINE',root_id,'malformed_procurement_pair'
  FROM fleetops.purchase_order_line_corrections_anomalies
UNION ALL SELECT org_id,'RECEIPT_LINE',root_id,'malformed_receipt_pair'
  FROM fleetops.receipt_line_corrections_anomalies
UNION ALL SELECT org_id,'EXCEPTION',exception_id,'workflow_projection_or_history'
  FROM fleetops.exception_workflow_anomalies
UNION ALL SELECT org_id,'RECEIPT',entity_id,issue FROM fleetops.receipt_evaluation_anomalies;
GRANT SELECT ON fleetops.receipt_evaluation_anomalies,fleetops.correction_anomalies TO fleetops_app;
"""
