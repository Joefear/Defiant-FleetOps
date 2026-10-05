"""Aggregate established reconciliation authorities without writes or mixed snapshots."""

from sqlalchemy import Connection, text

from fleetops.domain.asset_facts import RECONCILIATION_SQL
from fleetops.domain.corrections import LIFECYCLE_ISSUES_SQL, classify_lifecycle_issues

AGGREGATE_SQL = text(
    """
SELECT
  COALESCE((SELECT jsonb_agg(to_jsonb(a)) FROM (
"""
    + RECONCILIATION_SQL.text
    + """
  ) a),'[]'::jsonb) AS assets,
  COALESCE((SELECT jsonb_agg(to_jsonb(c)) FROM (
    SELECT DISTINCT * FROM fleetops.correction_anomalies ORDER BY entity_type,entity_id,issue
  ) c),'[]'::jsonb) AS corrections,
  COALESCE((SELECT jsonb_agg(to_jsonb(l)) FROM (
"""
    + LIFECYCLE_ISSUES_SQL
    + """
  ) l),'[]'::jsonb) AS lifecycle
"""
)


def reconciliation_health(connection: Connection):
    """Reuse Slice 5/6/7/10 checks, including the union of global version producers.

    Per-class gaps remain legal. One affected entity counts once even if several
    checks report it. Categories are preserved for diagnosis and nothing repairs
    history/projections. Python still owns the lifecycle graph under ADR-001.
    """
    row = (
        connection.execute(AGGREGATE_SQL, {"invalid_lifecycle_ids": [], "asset_id": None})
        .mappings()
        .one()
    )
    asset_rows = row["assets"]
    lifecycle = classify_lifecycle_issues(row["lifecycle"])
    by_asset = {str(asset["asset_id"]): asset for asset in asset_rows}
    for issue in lifecycle:
        identity = str(issue["entity_id"])
        if identity in by_asset:
            by_asset[identity]["discrepancies"].append(issue["issue"])
        else:
            # Legal projection values can still depend on an illegal corrected
            # edge; report that Asset even when the SQL checks alone were clean.
            asset_rows.append({"asset_id": identity, "discrepancies": [issue["issue"]]})
    correction_rows = row["corrections"] + lifecycle
    affected_assets = {str(asset["asset_id"]) for asset in asset_rows}
    affected = {(issue["entity_type"], str(issue["entity_id"])) for issue in correction_rows}
    affected.update(("ASSET", identity) for identity in affected_assets)
    return {
        "discrepancy_count": len(affected),
        "affected_asset_count": len(affected_assets),
        "affected_record_count": len(
            affected - {("ASSET", identity) for identity in affected_assets}
        ),
        "assets": asset_rows,
        "corrections": correction_rows,
    }
