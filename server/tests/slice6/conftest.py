"""Controlled creation and independent persisted snapshots for Slice 6 proofs."""

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5 import conftest as asset_fixtures
from server.tests.slice5.test_authentication_boundary import other_human as other_human_fixture
from sqlalchemy import select, text
from uuid6 import uuid7

from fleetops.db.metadata import (
    asset_custody_changes,
    asset_initial_facts,
    asset_movements,
    asset_ownership_changes,
    asset_transitions,
    assets,
)
from fleetops.db.tenancy import apply_tenant_policy

space_data = asset_fixtures.space_data
space_password_hash = asset_fixtures.space_password_hash
seed_asset = asset_fixtures.seed_asset
asset_data = asset_fixtures.asset_data
asset_client = asset_fixtures.asset_client
# Expose the established real second-HUMAN credential fixture to this slice.
other_human = other_human_fixture


@dataclass(frozen=True)
class FactKind:
    """Separate physical history classes share one global Asset version."""

    name: str
    function: str
    table: object
    projection: str
    initial: str
    from_field: str
    to_field: str
    correction: str
    route: str
    target_attribute: str


MOVE = FactKind(
    "movement",
    "move_asset",
    asset_movements,
    "current_location_id",
    "initial_location_id",
    "from_location_id",
    "to_location_id",
    "corrects_movement_id",
    "movements",
    "other_location_id",
)
CUSTODY = FactKind(
    "custody",
    "change_custody",
    asset_custody_changes,
    "custodian_party_id",
    "initial_custodian_party_id",
    "from_custodian_party_id",
    "to_custodian_party_id",
    "corrects_custody_change_id",
    "custody-changes",
    "other_manufacturer_id",
)
OWNERSHIP = FactKind(
    "ownership",
    "change_ownership",
    asset_ownership_changes,
    "owner_party_id",
    "initial_owner_party_id",
    "from_owner_party_id",
    "to_owner_party_id",
    "corrects_ownership_change_id",
    "ownership-changes",
    "other_manufacturer_id",
)
KINDS = (MOVE, CUSTODY, OWNERSHIP)
TABLES = (asset_initial_facts, *(kind.table for kind in KINDS))
HISTORY_TABLES = (asset_transitions, *(kind.table for kind in KINDS))


def for_asset(tenant, asset_id):
    """Reuse the credential/tenant while addressing a separately declared fixture Asset."""
    return SimpleNamespace(**(vars(tenant) | {"asset_id": asset_id}))


def call_fact(connection, tenant, kind, **changes):
    """Invoke a real atomic function; no performing Actor argument exists."""
    values = (
        dict(
            asset_id=tenant.asset_id,
            expected_version=1,
            target=getattr(tenant, kind.target_attribute),
            reason="Physical fact proof",
            occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
            client_op_id=None,
            history_id=uuid7(),
        )
        | changes
    )
    return dict(
        connection.execute(
            text(f"""
        SELECT * FROM fleetops.{kind.function}(
            :asset_id, :expected_version, :target, :reason,
            :occurred_at, :client_op_id, :history_id)
    """),
            values,
        )
        .mappings()
        .one()
    )


def runtime_fact(connection, tenant, kind, **changes):
    """Each invocation is an actual app-role transaction, committed only on success."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return call_fact(connection, tenant, kind, **changes)


def baseline_values(tenant, **changes):
    """Declared creation inputs, never a SELECT of a potentially corrupt projection."""
    return (
        dict(
            asset_id=tenant.asset_id,
            org_id=tenant.org_id,
            initial_owner_party_id=tenant.vendor_id,
            initial_custodian_party_id=tenant.party_id,
            initial_location_id=tenant.location_id,
            actor_id=tenant.actor_id,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        | changes
    )


def history_values(tenant, kind, **changes):
    """Privileged corruption/constraint inputs use real same-tenant references by default."""
    return (
        dict(
            id=uuid7(),
            asset_id=tenant.asset_id,
            org_id=tenant.org_id,
            result_version=2,
            actor_id=tenant.actor_id,
            occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
            reason="Privileged test history",
            **{
                kind.from_field: tenant.location_id if kind is MOVE else tenant.party_id,
                kind.to_field: getattr(tenant, kind.target_attribute),
            },
        )
        | changes
    )


@pytest.fixture
def fact_snapshot(migrator_connection):
    """Compare all persisted Asset facts, including every history class and the baseline."""

    def read(tenant):
        asset = dict(
            migrator_connection.execute(select(assets).where(assets.c.id == tenant.asset_id))
            .mappings()
            .one()
        )
        baseline = (
            migrator_connection.execute(
                select(asset_initial_facts).where(asset_initial_facts.c.asset_id == tenant.asset_id)
            )
            .mappings()
            .one_or_none()
        )
        result = {"asset": asset, "initial": dict(baseline) if baseline is not None else None}
        for table in HISTORY_TABLES:
            result[table.name] = [
                dict(row)
                for row in migrator_connection.execute(
                    select(table)
                    .where(table.c.asset_id == tenant.asset_id)
                    .order_by(table.c.result_version)
                ).mappings()
            ]
        migrator_connection.rollback()
        return result

    return read


@pytest.fixture
def fact_dml(migrator_connection, app_connection):
    """Temporarily isolate RLS/attribution from denied production DML, then restore ACLs."""
    for table in TABLES:
        migrator_connection.exec_driver_sql(
            f"GRANT INSERT, UPDATE, DELETE ON fleetops.{table.name} TO fleetops_app"
        )
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for table in TABLES:
            apply_tenant_policy(migrator_connection, table, privileges=("SELECT",))
        migrator_connection.commit()
