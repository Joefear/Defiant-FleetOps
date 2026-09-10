"""Atomic physical facts and read-only reconciliation (D3/D7, ADR-005/006/007).

Creation remains receiving-owned. The baseline has no production writer here and
is excluded from global version production; it only witnesses the initial facts.
"""

from uuid import UUID

from sqlalchemy import Connection, select, text
from uuid6 import uuid7

from fleetops.db.metadata import asset_custody_changes, asset_movements, asset_ownership_changes
from fleetops.domain.assets import _constraints, get_asset


def _list_history(connection: Connection, asset_id: UUID, table):
    """Require a visible Asset, then read its class history in authoritative version order."""
    get_asset(connection, asset_id)
    return (
        connection.execute(
            select(table).where(table.c.asset_id == asset_id).order_by(table.c.result_version)
        )
        .mappings()
        .all()
    )


def list_movements(connection: Connection, asset_id: UUID):
    """Read location changes; creation facts remain an independent immutable baseline."""
    return _list_history(connection, asset_id, asset_movements)


def list_custody_changes(connection: Connection, asset_id: UUID):
    """Read custody without inferring ownership from possession."""
    return _list_history(connection, asset_id, asset_custody_changes)


def list_ownership_changes(connection: Connection, asset_id: UUID):
    """Read ownership without inferring custody from title."""
    return _list_history(connection, asset_id, asset_ownership_changes)


def _apply(connection: Connection, asset_id: UUID, statement, values: dict):
    """The database owns lock/admission/Actor checks; the request transaction owns commit."""
    with _constraints():
        return (
            connection.execute(statement, dict(values, asset_id=asset_id, history_id=uuid7()))
            .mappings()
            .one()
        )


def move_asset(connection: Connection, asset_id: UUID, *, values: dict):
    """Append an authenticated location fact, including explicitly unknown location."""
    return _apply(
        connection,
        asset_id,
        text("""
        SELECT * FROM fleetops.move_asset(
            :asset_id, :expected_version, :to_location_id, :reason,
            :occurred_at, NULL, :history_id)
    """),
        values,
    )


def change_custody(connection: Connection, asset_id: UUID, *, values: dict):
    """Custody changes independently; the global version still competes with all classes."""
    return _apply(
        connection,
        asset_id,
        text("""
        SELECT * FROM fleetops.change_custody(
            :asset_id, :expected_version, :to_custodian_party_id, :reason,
            :occurred_at, NULL, :history_id)
    """),
        values,
    )


def change_ownership(connection: Connection, asset_id: UUID, *, values: dict):
    """Transfer ownership with a required Party and credential-derived performing Actor."""
    return _apply(
        connection,
        asset_id,
        text("""
        SELECT * FROM fleetops.change_ownership(
            :asset_id, :expected_version, :to_owner_party_id, :reason,
            :occurred_at, NULL, :history_id)
    """),
        values,
    )


# One statement observes one MVCC snapshot across projections and all history classes.
# Missing versions are ranges between observed versions and boundary sentinels, not a
# generate_series up to a possibly corrupt INT_MAX projection. No version register exists.
RECONCILIATION_SQL = text("""
WITH tenant_assets AS (
    SELECT * FROM fleetops.assets
), events AS (
    SELECT h.org_id, h.asset_id, h.result_version FROM fleetops.asset_transitions h
      JOIN tenant_assets a ON a.org_id = h.org_id AND a.id = h.asset_id
    UNION ALL
    SELECT h.org_id, h.asset_id, h.result_version FROM fleetops.asset_movements h
      JOIN tenant_assets a ON a.org_id = h.org_id AND a.id = h.asset_id
    UNION ALL
    SELECT h.org_id, h.asset_id, h.result_version FROM fleetops.asset_custody_changes h
      JOIN tenant_assets a ON a.org_id = h.org_id AND a.id = h.asset_id
    UNION ALL
    SELECT h.org_id, h.asset_id, h.result_version FROM fleetops.asset_ownership_changes h
      JOIN tenant_assets a ON a.org_id = h.org_id AND a.id = h.asset_id
    UNION ALL
    SELECT h.org_id, h.asset_id, h.result_version FROM fleetops.asset_assignment_events h
      JOIN tenant_assets a ON a.org_id = h.org_id AND a.id = h.asset_id
), version_counts AS (
    SELECT org_id, asset_id, result_version, count(*) AS producers
    FROM events GROUP BY org_id, asset_id, result_version
), points AS (
    SELECT org_id, id AS asset_id, 0::bigint AS v FROM tenant_assets
    UNION ALL
    SELECT org_id, id, version::bigint + 1 FROM tenant_assets
    UNION ALL
    SELECT c.org_id, c.asset_id, c.result_version::bigint FROM version_counts c
    JOIN tenant_assets a ON a.org_id = c.org_id AND a.id = c.asset_id
    WHERE c.result_version BETWEEN 1 AND a.version
), ordered_points AS (
    SELECT org_id, asset_id, v,
           lag(v) OVER (PARTITION BY org_id, asset_id ORDER BY v) AS previous
    FROM points
), gaps AS (
    SELECT org_id, asset_id,
           jsonb_agg(jsonb_build_array(previous + 1, v - 1) ORDER BY v) AS missing_ranges
    FROM ordered_points WHERE v > previous + 1 GROUP BY org_id, asset_id
), global_checks AS (
    SELECT a.org_id, a.id AS asset_id,
        COALESCE(array_agg(c.result_version ORDER BY c.result_version)
            FILTER (WHERE c.producers > 1), '{}'::integer[]) AS duplicate_versions,
        COALESCE(array_agg(c.result_version ORDER BY c.result_version)
            FILTER (WHERE c.result_version > a.version), '{}'::integer[]) AS ahead_versions
    FROM tenant_assets a
    LEFT JOIN version_counts c ON c.org_id = a.org_id AND c.asset_id = a.id
    GROUP BY a.org_id, a.id
), facts AS (
    SELECT a.id AS asset_id, a.org_id, a.current_state, a.version,
           a.current_location_id, a.custodian_party_id, a.owner_party_id,
           b.asset_id IS NOT NULL AS initial_facts_present,
           b.initial_location_id, b.initial_custodian_party_id, b.initial_owner_party_id,
           t.id AS transition_id, t.to_state AS latest_state,
           t.result_version AS latest_result_version,
           m.id AS movement_id, m.to_location_id AS latest_location_id,
           m.result_version AS latest_movement_result_version,
           c.id AS custody_id, c.to_custodian_party_id AS latest_custodian_party_id,
           c.result_version AS latest_custody_result_version,
           o.id AS ownership_id, o.to_owner_party_id AS latest_owner_party_id,
           o.result_version AS latest_ownership_result_version,
           a.current_assignment_id,
           w.asset_id IS NOT NULL AS initial_assignment_facts_present,
           e.id AS latest_assignment_event_id,
           e.result_version AS latest_assignment_result_version,
           e.to_assignee_type AS latest_assignee_type,
           e.to_assignee_id AS latest_assignee_id,
           target.id IS NOT NULL AND target.asset_id = a.id AS assignment_target_valid
    FROM tenant_assets a
    LEFT JOIN fleetops.asset_initial_facts b ON b.org_id = a.org_id AND b.asset_id = a.id
    LEFT JOIN fleetops.asset_initial_assignment_facts w
      ON w.org_id = a.org_id AND w.asset_id = a.id
    LEFT JOIN fleetops.asset_assignment_events target
      ON target.org_id = a.org_id AND target.id = a.current_assignment_id
    LEFT JOIN LATERAL (
        SELECT id, to_state, result_version FROM fleetops.asset_transitions
        WHERE org_id = a.org_id AND asset_id = a.id ORDER BY result_version DESC LIMIT 1
    ) t ON true
    LEFT JOIN LATERAL (
        SELECT id, to_location_id, result_version FROM fleetops.asset_movements
        WHERE org_id = a.org_id AND asset_id = a.id ORDER BY result_version DESC LIMIT 1
    ) m ON true
    LEFT JOIN LATERAL (
        SELECT id, to_custodian_party_id, result_version FROM fleetops.asset_custody_changes
        WHERE org_id = a.org_id AND asset_id = a.id ORDER BY result_version DESC LIMIT 1
    ) c ON true
    LEFT JOIN LATERAL (
        SELECT id, to_owner_party_id, result_version FROM fleetops.asset_ownership_changes
        WHERE org_id = a.org_id AND asset_id = a.id ORDER BY result_version DESC LIMIT 1
    ) o ON true
    LEFT JOIN LATERAL (
        SELECT id, to_assignee_type, to_assignee_id, result_version
        FROM fleetops.asset_assignment_events
        WHERE org_id = a.org_id AND asset_id = a.id ORDER BY result_version DESC LIMIT 1
    ) e ON true
), checked AS (
    SELECT f.*,
        COALESCE(g.missing_ranges, '[]'::jsonb) AS global_missing_version_ranges,
        v.duplicate_versions AS global_duplicate_versions,
        v.ahead_versions AS global_ahead_versions,
        array_remove(ARRAY[
            CASE WHEN transition_id IS NULL THEN 'missing_history' END,
            CASE WHEN transition_id IS NOT NULL AND current_state <> latest_state
                 THEN 'state_mismatch' END,
            CASE WHEN latest_result_version > version THEN 'history_version_ahead' END,
            CASE WHEN NOT initial_facts_present THEN 'missing_initial_facts' END,
            CASE WHEN movement_id IS NULL AND initial_facts_present
                 AND current_location_id IS DISTINCT FROM initial_location_id
                 THEN 'initial_location_projection_mismatch' END,
            CASE WHEN custody_id IS NULL AND initial_facts_present
                 AND custodian_party_id IS DISTINCT FROM initial_custodian_party_id
                 THEN 'initial_custody_projection_mismatch' END,
            CASE WHEN ownership_id IS NULL AND initial_facts_present
                 AND owner_party_id IS DISTINCT FROM initial_owner_party_id
                 THEN 'initial_ownership_projection_mismatch' END,
            CASE WHEN movement_id IS NOT NULL
                 AND current_location_id IS DISTINCT FROM latest_location_id
                 THEN 'movement_projection_mismatch' END,
            CASE WHEN custody_id IS NOT NULL
                 AND custodian_party_id IS DISTINCT FROM latest_custodian_party_id
                 THEN 'custody_projection_mismatch' END,
            CASE WHEN ownership_id IS NOT NULL
                 AND owner_party_id IS DISTINCT FROM latest_owner_party_id
                 THEN 'ownership_projection_mismatch' END,
            CASE WHEN latest_movement_result_version > version
                 THEN 'movement_history_version_ahead' END,
            CASE WHEN latest_custody_result_version > version
                 THEN 'custody_history_version_ahead' END,
            CASE WHEN latest_ownership_result_version > version
                 THEN 'ownership_history_version_ahead' END,
            CASE WHEN NOT initial_assignment_facts_present
                 THEN 'missing_initial_assignment_facts' END,
            CASE WHEN latest_assignment_event_id IS NULL AND current_assignment_id IS NOT NULL
                 THEN 'assignment_projection_without_history' END,
            CASE WHEN latest_assignment_event_id IS NOT NULL AND current_assignment_id
                 IS DISTINCT FROM CASE WHEN latest_assignee_id IS NOT NULL
                                      THEN latest_assignment_event_id END
                 THEN 'assignment_projection_mismatch' END,
            CASE WHEN current_assignment_id IS NOT NULL AND NOT assignment_target_valid
                 THEN 'assignment_target_invalid' END,
            CASE WHEN latest_assignment_result_version > version
                 THEN 'assignment_history_version_ahead' END,
            CASE WHEN g.missing_ranges IS NOT NULL THEN 'global_version_missing' END,
            CASE WHEN cardinality(v.duplicate_versions) > 0 THEN 'global_version_duplicate' END,
            CASE WHEN cardinality(v.ahead_versions) > 0 THEN 'global_history_version_ahead' END
        ], NULL) AS discrepancies
    FROM facts f
    JOIN global_checks v ON v.org_id = f.org_id AND v.asset_id = f.asset_id
    LEFT JOIN gaps g ON g.org_id = f.org_id AND g.asset_id = f.asset_id
)
SELECT * FROM checked WHERE cardinality(discrepancies) > 0 ORDER BY asset_id
""")


def reconcile_assets(connection: Connection):
    """Report initial, latest-class and global disagreements without repair.

    ADR-007/008 creation authority applies only until the relevant class changes.
    Both witnesses' continued existence is checked separately from current values,
    including after later events. Neither witness nor configuration produces a version.
    Slice 5 state fields/categories retain their meaning; per-class version gaps are valid.
    """
    return connection.execute(RECONCILIATION_SQL).mappings().all()
