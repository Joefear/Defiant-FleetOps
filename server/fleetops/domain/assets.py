"""Bounded Asset reads, descriptive edits, lifecycle admission and state reconciliation.

Receiving owns creation. No production helper here can manufacture an Asset or its
initial history, and no identifier-write path can detach capture from that provenance.
"""

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_identifiers, asset_transitions, assets
from fleetops.domain.lifecycle import AssetState, LifecycleInvalid, validate_edge


class AssetNotFound(Exception):
    """Missing and cross-tenant targets have the same public failure."""


class AssetConflict(Exception):
    """Stale version, inconsistent history/projection, or duplicate display tag."""


class AssetInvalid(Exception):
    """Invalid facts or attribution; database details never escape the request boundary."""


@contextmanager
def _constraints():
    """Translate only known failures; the enclosing request transaction rolls back."""
    try:
        yield
    except DBAPIError as error:
        code = getattr(error.orig, "sqlstate", None)
        if code == "P0002":
            raise AssetNotFound("Asset not found") from error
        if code in {"40001", "P0001", "23505"}:
            raise AssetConflict(
                "Asset version, history, or tag conflicts with this operation"
            ) from error
        if code in {"23502", "23503", "23514", "42501", "22003"}:
            raise AssetInvalid("Invalid Asset operation or attribution") from error
        raise


def list_assets(connection: Connection):
    """Return Assets through the authenticated runtime transaction's RLS."""
    return connection.execute(select(assets).order_by(assets.c.id)).mappings().all()


def get_asset(connection: Connection, asset_id: UUID):
    """Resolve only FleetOps identity, with no tag/serial identity shortcut."""
    row = connection.execute(select(assets).where(assets.c.id == asset_id)).mappings().one_or_none()
    if row is None:
        raise AssetNotFound("Asset not found")
    return row


def patch_asset(connection: Connection, asset_id: UUID, *, performer_id: UUID, values: dict):
    """Record authenticated attribution with descriptive edits; global version is untouched."""
    if not values or not values.keys() <= {"asset_tag", "description"}:
        raise AssetInvalid("Only asset_tag and description may be edited")
    with _constraints():
        row = (
            connection.execute(
                assets.update()
                .where(assets.c.id == asset_id)
                .values(
                    **values,
                    updated_by_actor_id=performer_id,
                    updated_at=text("statement_timestamp()"),
                )
                .returning(assets)
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise AssetNotFound("Asset not found")
    return row


def list_identifiers(connection: Connection, asset_id: UUID):
    """Identifiers are readable here; receiving owns their eventual production capture."""
    get_asset(connection, asset_id)
    return (
        connection.execute(
            select(asset_identifiers)
            .where(asset_identifiers.c.asset_id == asset_id)
            .order_by(asset_identifiers.c.id)
        )
        .mappings()
        .all()
    )


def list_transitions(connection: Connection, asset_id: UUID):
    """Global result versions order state history even when occurrence clocks disagree."""
    get_asset(connection, asset_id)
    return (
        connection.execute(
            select(asset_transitions)
            .where(asset_transitions.c.asset_id == asset_id)
            .order_by(asset_transitions.c.result_version)
        )
        .mappings()
        .all()
    )


def transition_asset(connection: Connection, asset_id: UUID, *, values: dict):
    """Check Python-owned legality, then invoke the atomic privileged history boundary.

    ON_HOLD is dynamic: hold the same Asset row lock while reading its latest entry
    state and invoking SQL. Otherwise a caller guessing a future expected_version could
    race a complete leave/re-enter cycle and authorize an exit from obsolete history.
    The function itself always locks and validates the version, including direct calls.
    """
    from_state, to_state = values["from_state"], values["to_state"]
    if from_state != AssetState.ON_HOLD:
        validate_edge(from_state, to_state)
    # D17 / ADR-004: a legal graph edge cannot substitute for disposal evidence.
    if to_state == AssetState.RETIRED:
        raise LifecycleInvalid("Retirement requires verifiable disposal evidence")
    if from_state == AssetState.ON_HOLD:
        target = connection.execute(
            select(assets.c.id).where(assets.c.id == asset_id).with_for_update()
        ).scalar_one_or_none()
        if target is None:
            raise AssetNotFound("Asset not found")
        latest = (
            connection.execute(
                select(asset_transitions)
                .where(asset_transitions.c.asset_id == asset_id)
                .order_by(asset_transitions.c.result_version.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if latest is None or latest["to_state"] != AssetState.ON_HOLD:
            raise AssetConflict("Authoritative ON_HOLD history is missing or inconsistent")
        validate_edge(from_state, to_state, previous_state=latest["from_state"])
    with _constraints():
        return (
            connection.execute(
                text("""
            SELECT * FROM fleetops.transition_asset(
                :asset_id, :expected_version, :from_state, :to_state, :reason,
                :occurred_at, NULL, NULL, :transition_id)
        """),
                dict(values, asset_id=asset_id, transition_id=uuid7()),
            )
            .mappings()
            .one()
        )


def reconcile_state(connection: Connection):
    """Compare each projection to history, including Assets with no history (D3).

    ADR-005 permits lower state-history versions. Only a history version ahead of the
    Asset is impossible here; complete global-version reconciliation belongs to the
    slices introducing other operation classes, without a shared version register.
    """
    return (
        connection.execute(
            text("""
        SELECT a.id AS asset_id, a.current_state, a.version,
               h.to_state AS latest_state, h.result_version AS latest_result_version,
               array_remove(ARRAY[
                   CASE WHEN h.id IS NULL THEN 'missing_history' END,
                   CASE WHEN h.id IS NOT NULL AND a.current_state <> h.to_state
                        THEN 'state_mismatch' END,
                   CASE WHEN h.result_version > a.version THEN 'history_version_ahead' END
               ], NULL) AS discrepancies
        FROM fleetops.assets a
        LEFT JOIN LATERAL (
            SELECT t.id, t.to_state, t.result_version FROM fleetops.asset_transitions t
            WHERE t.org_id = a.org_id AND t.asset_id = a.id
            ORDER BY t.result_version DESC LIMIT 1
        ) h ON true
        WHERE h.id IS NULL OR a.current_state <> h.to_state OR h.result_version > a.version
        ORDER BY a.id
    """)
        )
        .mappings()
        .all()
    )
