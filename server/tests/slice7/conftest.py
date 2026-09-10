"""Controlled creation and independent persisted snapshots for ADR-008 proofs."""

from datetime import UTC, datetime

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice6 import conftest as facts
from sqlalchemy import select, text
from uuid6 import uuid7

from fleetops.db.metadata import (
    asset_assignment_events,
    asset_configurations,
    asset_initial_assignment_facts,
)

space_data = facts.space_data
space_password_hash = facts.space_password_hash
seed_asset = facts.seed_asset
asset_data = facts.asset_data
asset_client = facts.asset_client
other_human = facts.other_human
fact_snapshot = facts.fact_snapshot

TABLES = (asset_initial_assignment_facts, asset_assignment_events, asset_configurations)
SEQUENCE = "fleetops.asset_configurations_configuration_seq_seq"


def witness_values(tenant, **changes):
    """Declared creation input, independent of the current projection."""
    return (
        dict(
            asset_id=tenant.asset_id,
            org_id=tenant.org_id,
            actor_id=tenant.actor_id,
            occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        | changes
    )


def event_values(tenant, **changes):
    """Explicit privileged corruption/constraint fixture values, not production admission."""
    return (
        dict(
            id=uuid7(),
            org_id=tenant.org_id,
            asset_id=tenant.asset_id,
            result_version=2,
            from_assignee_type=None,
            from_assignee_id=None,
            to_assignee_type="LOCATION",
            to_assignee_id=tenant.location_id,
            actor_id=tenant.actor_id,
            occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
            reason="Assignment proof",
        )
        | changes
    )


def config_values(tenant, **changes):
    """Omit every database-controlled column, as actual ordinary runtime INSERT must."""
    return (
        dict(
            id=uuid7(),
            org_id=tenant.org_id,
            asset_id=tenant.asset_id,
            image_name="Fleet image",
            image_version="1.0",
            config_profile="station",
            notes="",
            applied_at=datetime(2026, 5, 1, tzinfo=UTC),
        )
        | changes
    )


def call_assignment(connection, tenant, *, unassign=False, **changes):
    """Invoke the real SQL boundary with a service-generated identity, no Actor input."""
    values = (
        dict(
            asset_id=tenant.asset_id,
            expected_version=1,
            assignee_type="LOCATION",
            assignee_id=tenant.location_id,
            reason="Assignment proof",
            occurred_at=datetime(2026, 5, 1, tzinfo=UTC),
            client_op_id=None,
            event_id=uuid7(),
        )
        | changes
    )
    target = "" if unassign else ":assignee_type, :assignee_id,"
    function = "unassign_asset" if unassign else "assign_asset"
    return dict(
        connection.execute(
            text(f"""
        SELECT * FROM fleetops.{function}(:asset_id, :expected_version, {target}
            :reason, :occurred_at, :client_op_id, :event_id)
    """),
            values,
        )
        .mappings()
        .one()
    )


def runtime_assignment(connection, tenant, **changes):
    """Actual app connection owns one transaction; failures roll it back."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return call_assignment(connection, tenant, **changes)


def runtime_configuration(connection, tenant, **changes):
    """Exercise permitted raw SQL INSERT instead of relying on HTTP restrictions."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return dict(
            connection.execute(
                asset_configurations.insert()
                .values(config_values(tenant, **changes))
                .returning(asset_configurations)
            )
            .mappings()
            .one()
        )


@pytest.fixture
def assignment_snapshot(fact_snapshot, migrator_connection):
    """Read all persisted classes and both witnesses independently from operation results."""

    def read(tenant):
        result = fact_snapshot(tenant)
        for table in TABLES:
            order = (
                table.c.result_version
                if table is asset_assignment_events
                else table.c.configuration_seq
                if table is asset_configurations
                else table.c.asset_id
            )
            result[table.name] = [
                dict(row)
                for row in migrator_connection.execute(
                    select(table).where(table.c.asset_id == tenant.asset_id).order_by(order)
                ).mappings()
            ]
        migrator_connection.rollback()
        return result

    return read
