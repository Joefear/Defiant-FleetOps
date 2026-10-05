"""Typed correction admission; effective facts and raw audit remain separate (ADR-013)."""

from uuid import UUID

from sqlalchemy import Connection, select, text
from uuid6 import uuid7

from fleetops.db.metadata import asset_transitions, assets
from fleetops.domain.assets import AssetConflict, AssetInvalid, AssetNotFound, _constraints
from fleetops.domain.lifecycle import AssetState, LifecycleInvalid, validate_edge
from fleetops.evidence.service import verify_asset_evidence


def _correct(connection, asset_id, root_id, values, statement):
    """Supply server UUIDs to a fixed typed atomic boundary in the request transaction."""
    # Retain the same Asset lock while checking Python-owned lifecycle authority.
    connection.execute(select(assets.c.id).where(assets.c.id == asset_id).with_for_update())
    if lifecycle_correction_issues(connection, asset_id=asset_id):
        raise AssetConflict("Malformed corrected lifecycle authority")
    with _constraints():
        return (
            connection.execute(
                text(statement),
                dict(
                    values,
                    asset_id=asset_id,
                    root_id=root_id,
                    pair_id=uuid7(),
                    reversal_id=uuid7(),
                    corrected_id=uuid7(),
                    occurred_at=values.get("occurred_at"),
                ),
            )
            .mappings()
            .one()
        )


def correct_movement(connection: Connection, asset_id: UUID, root_id: UUID, *, values: dict):
    """Replace the recorded destination from the original pre-root physical source."""
    return _correct(
        connection,
        asset_id,
        root_id,
        values,
        """
      SELECT * FROM fleetops.correct_movement(:asset_id,:root_id,:expected_version,
        :to_location_id,:reason,:correction_occurred_at,:occurred_at,
        :pair_id,:reversal_id,:corrected_id)
    """,
    )


def correct_custody(connection: Connection, asset_id: UUID, root_id: UUID, *, values: dict):
    """Custody correction changes neither title nor location."""
    return _correct(
        connection,
        asset_id,
        root_id,
        values,
        """
      SELECT * FROM fleetops.correct_custody(:asset_id,:root_id,:expected_version,
        :to_custodian_party_id,:reason,:correction_occurred_at,:occurred_at,
        :pair_id,:reversal_id,:corrected_id)
    """,
    )


def correct_ownership(connection: Connection, asset_id: UUID, root_id: UUID, *, values: dict):
    """Record the intended represented owner without inventing a later transfer."""
    return _correct(
        connection,
        asset_id,
        root_id,
        values,
        """
      SELECT * FROM fleetops.correct_ownership(:asset_id,:root_id,:expected_version,
        :to_owner_party_id,:reason,:correction_occurred_at,:occurred_at,
        :pair_id,:reversal_id,:corrected_id)
    """,
    )


def correct_assignment(connection: Connection, asset_id: UUID, root_id: UUID, *, values: dict):
    """Replace one assignment assertion; reversal is never a physical unassignment."""
    return _correct(
        connection,
        asset_id,
        root_id,
        values,
        """
      SELECT * FROM fleetops.correct_assignment(:asset_id,:root_id,:expected_version,
        :to_assignee_type,:to_assignee_id,:reason,:correction_occurred_at,:occurred_at,
        :pair_id,:reversal_id,:corrected_id)
    """,
    )


def correct_transition(
    connection: Connection, asset_id: UUID, root_id: UUID, *, values: dict, storage=None
):
    """Validate Python's graph using effective pre-root authority under the shared lock.

    ON_HOLD remembers the effective entry edge. The same lock remains held through
    SQL admission, so validation cannot race a correction or an ordinary operation.
    """
    locked = (
        connection.execute(select(assets).where(assets.c.id == asset_id).with_for_update())
        .mappings()
        .one_or_none()
    )
    if locked is None:
        raise AssetNotFound("Asset not found")
    if locked["version"] != values["expected_version"]:
        raise AssetConflict("Asset version is stale")
    root = (
        connection.execute(
            select(asset_transitions).where(
                asset_transitions.c.asset_id == asset_id, asset_transitions.c.id == root_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if root is None:
        raise AssetNotFound("History not found")
    if root["correction_role"] != "NONE":
        raise AssetInvalid("Correction requires an ordinary root")
    prior = (
        connection.execute(
            text("""
      SELECT h.* FROM fleetops.effective_asset_transitions h
      JOIN fleetops.asset_transitions r ON r.org_id=h.org_id AND r.asset_id=h.asset_id
      WHERE r.id=:root_id AND r.asset_id=:asset_id AND r.correction_role='NONE'
        AND h.result_version<r.result_version ORDER BY h.result_version DESC LIMIT 1
    """),
            dict(root_id=root_id, asset_id=asset_id),
        )
        .mappings()
        .one_or_none()
    )
    if prior is None:
        raise LifecycleInvalid("A lifecycle correction requires a replaceable ordinary edge")
    validate_edge(
        AssetState(prior["to_state"]), values["to_state"], previous_state=prior["from_state"]
    )
    if values["to_state"] == AssetState.RETIRED and values.get("evidence_ref") is None:
        raise LifecycleInvalid("Retirement requires verifiable disposal evidence")
    verify_asset_evidence(
        connection,
        storage,
        asset_id,
        values.get("evidence_ref"),
        role="DISPOSAL_EVIDENCE" if values["to_state"] == AssetState.RETIRED else None,
        required=values["to_state"] == AssetState.RETIRED,
    )
    return _correct(
        connection,
        asset_id,
        root_id,
        dict(values, evidence_ref=values.get("evidence_ref")),
        """
      SELECT * FROM fleetops.correct_transition(:asset_id,:root_id,:expected_version,
        :to_state,:reason,:correction_occurred_at,:occurred_at,
        :pair_id,:reversal_id,:corrected_id,:evidence_ref)
    """,
    )


def effective_events(rows, root_field):
    """Select effective facts from raw audit after validating every represented pair.

    This read does not repair malformed history or infer a head from clocks. Database
    constraints protect writes; validation here also exposes privileged corruption.
    """
    roots = {row["id"]: row for row in rows if row["correction_role"] == "NONE"}
    pairs = {}
    for row in rows:
        if row["correction_role"] == "NONE":
            if row[root_field] is not None or row["correction_generation"] != 0:
                raise AssetConflict("Malformed ordinary history")
            continue
        root = roots.get(row[root_field])
        if root is None or root["asset_id"] != row["asset_id"]:
            raise AssetConflict("Malformed correction root")
        pairs.setdefault((row[root_field], row["correction_generation"]), []).append(row)
    for (root_id, generation), pair in sorted(pairs.items(), key=lambda item: item[0][1]):
        if {row["correction_role"] for row in pair} != {"REVERSAL", "CORRECTED"} or len(pair) != 2:
            raise AssetConflict("Incomplete correction pair")
        previous = roots[root_id]
        if generation != previous["correction_generation"] + 1:
            raise AssetConflict("Noncontiguous correction generations")
        for field in ("correction_pair_id", "reason", "actor_id", "correction_occurred_at"):
            if pair[0][field] != pair[1][field] or pair[0][field] is None:
                raise AssetConflict("Inconsistent correction pair metadata")
        reversal = next(row for row in pair if row["correction_role"] == "REVERSAL")
        corrected = next(row for row in pair if row["correction_role"] == "CORRECTED")
        if (
            reversal["result_version"] != previous["result_version"] + 1
            or corrected["result_version"] != reversal["result_version"] + 1
        ):
            raise AssetConflict("Invalid correction version provenance")
        roots[root_id] = corrected
    return sorted(roots.values(), key=lambda row: row["result_version"])


def audit_rows(rows, root_field):
    """Name the cancelled authority explicitly while preserving malformed raw evidence."""
    rows = [dict(row) for row in rows]
    corrected = {
        (row[root_field], row["correction_generation"]): row["id"]
        for row in rows
        if row["correction_role"] == "CORRECTED"
    }
    for row in rows:
        row["cancels_history_id"] = None
        if row["correction_role"] == "REVERSAL":
            generation = row["correction_generation"]
            row["cancels_history_id"] = (
                row[root_field]
                if generation == 1
                else corrected.get((row[root_field], generation - 1))
            )
    return rows


LIFECYCLE_ISSUES_SQL = """
      SELECT c.org_id,c.asset_id,c.to_state,p.from_state AS previous_state,p.to_state AS from_state,
        fleetops.valid_asset_evidence(c.org_id,c.evidence_ref,c.asset_id,'DISPOSAL_EVIDENCE')
          AS disposal_valid
      FROM fleetops.asset_transitions c
      JOIN fleetops.asset_transitions r ON r.org_id=c.org_id AND r.asset_id=c.asset_id
        AND r.id=c.corrects_transition_id
      LEFT JOIN LATERAL (
        SELECT h.from_state,h.to_state FROM fleetops.effective_asset_transitions h
        WHERE h.org_id=c.org_id AND h.asset_id=c.asset_id AND h.result_version<r.result_version
        ORDER BY h.result_version DESC LIMIT 1
      ) p ON true
      WHERE c.correction_role='CORRECTED'
        AND (CAST(:asset_id AS uuid) IS NULL OR c.asset_id=CAST(:asset_id AS uuid))
    """


def lifecycle_correction_issues(connection: Connection, *, asset_id=None):
    """Check every corrected lifecycle edge in Python, including superseded generations.

    The graph remains Python-owned. SQL supplies effective pre-root context, never
    interprets administrative REVERSAL as a physical edge, and never repairs history.
    """
    rows = connection.execute(
        text(LIFECYCLE_ISSUES_SQL),
        dict(asset_id=asset_id),
    ).mappings()
    return classify_lifecycle_issues(rows)


def classify_lifecycle_issues(rows):
    """Apply the same Python-owned lifecycle rules to one supplied snapshot."""
    invalid = {}
    for row in rows:
        try:
            if row["from_state"] is None or (
                row["to_state"] == AssetState.RETIRED and not row["disposal_valid"]
            ):
                raise LifecycleInvalid("Missing pre-root state or unsupported evidence")
            validate_edge(
                AssetState(row["from_state"]), row["to_state"], previous_state=row["previous_state"]
            )
        except (LifecycleInvalid, ValueError):
            invalid[row["asset_id"]] = dict(
                org_id=row["org_id"],
                entity_type="ASSET",
                entity_id=row["asset_id"],
                issue="illegal_corrected_lifecycle",
            )
    return list(invalid.values())
