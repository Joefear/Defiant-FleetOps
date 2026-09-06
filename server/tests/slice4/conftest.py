"""Populated tenant fixtures for space proofs; assertions use the runtime role."""

import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.auth import hash_password, token_digest
from fleetops.db.metadata import (
    actors,
    external_references,
    facilities,
    items,
    locations,
    metadata,
    organizations,
    parties,
    party_roles,
    sessions,
    users,
)
from fleetops.settings import Settings


@pytest.fixture(scope="session")
def space_password_hash():
    """These tests authenticate with bearer sessions; no reusable password is needed."""
    return hash_password(secrets.token_urlsafe(32))


@pytest.fixture
def space_data(migrator_connection, app_connection, space_password_hash):
    """Seed populated A/B tenants; privileged setup is never the asserted runtime operation."""
    tenants = []
    try:
        for label in ("A", "B"):
            tenant = SimpleNamespace(
                org_id=uuid7(),
                actor_id=uuid7(),
                user_id=uuid7(),
                party_id=uuid7(),
                other_manufacturer_id=uuid7(),
                vendor_id=uuid7(),
                role_id=uuid7(),
                item_id=uuid7(),
                reference_id=uuid7(),
                facility_id=uuid7(),
                other_facility_id=uuid7(),
                location_id=uuid7(),
                other_location_id=uuid7(),
                raw_token=secrets.token_urlsafe(32),
            )
            tenants.append(tenant)
            migrator_connection.execute(organizations.insert().values(id=tenant.org_id, name=label))
            migrator_connection.execute(
                actors.insert().values(
                    id=tenant.actor_id,
                    org_id=tenant.org_id,
                    type="HUMAN",
                    display_name=label,
                    created_by_actor_id=tenant.actor_id,
                )
            )
            for party_id, role, role_id in (
                (tenant.party_id, "MANUFACTURER", tenant.role_id),
                (tenant.other_manufacturer_id, "MANUFACTURER", uuid7()),
                (tenant.vendor_id, "VENDOR", uuid7()),
            ):
                migrator_connection.execute(
                    parties.insert().values(
                        id=party_id,
                        org_id=tenant.org_id,
                        display_name=role,
                        created_by_actor_id=tenant.actor_id,
                    )
                )
                migrator_connection.execute(
                    party_roles.insert().values(
                        id=role_id,
                        org_id=tenant.org_id,
                        party_id=party_id,
                        role=role,
                    )
                )
            migrator_connection.execute(
                users.insert().values(
                    id=tenant.user_id,
                    org_id=tenant.org_id,
                    actor_id=tenant.actor_id,
                    username="space-operator",
                    password_hash=space_password_hash,
                )
            )
            migrator_connection.execute(
                sessions.insert().values(
                    id=uuid7(),
                    org_id=tenant.org_id,
                    user_id=tenant.user_id,
                    token_digest=token_digest(tenant.raw_token),
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            migrator_connection.execute(
                items.insert().values(
                    id=tenant.item_id,
                    org_id=tenant.org_id,
                    manufacturer_party_id=tenant.party_id,
                    manufacturer_part_number="CAT-100",
                    revision="A",
                    description="Catalog fixture",
                    uom="EA",
                    serialized=True,
                    created_by_actor_id=tenant.actor_id,
                    updated_by_actor_id=tenant.actor_id,
                )
            )
            migrator_connection.execute(
                external_references.insert().values(
                    id=tenant.reference_id,
                    org_id=tenant.org_id,
                    entity_type="ITEM",
                    entity_id=tenant.item_id,
                    system="DigiKey",
                    reference_type="DISTRIBUTOR_PART",
                    external_value="123-456",
                    created_by_actor_id=tenant.actor_id,
                )
            )

            for facility_id, location_id in (
                (tenant.facility_id, tenant.location_id),
                (tenant.other_facility_id, tenant.other_location_id),
            ):
                migrator_connection.execute(
                    facilities.insert().values(
                        id=facility_id,
                        org_id=tenant.org_id,
                        name=label,
                        timezone="America/Chicago",
                        created_by_actor_id=tenant.actor_id,
                    )
                )
                migrator_connection.execute(
                    locations.insert().values(
                        id=location_id,
                        org_id=tenant.org_id,
                        facility_id=facility_id,
                        code="SITE",
                        name="Root",
                        kind="SITE",
                        created_by_actor_id=tenant.actor_id,
                    )
                )
        migrator_connection.commit()
        yield tuple(tenants)
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for table in reversed(metadata.sorted_tables):
            migrator_connection.execute(
                table.delete().where(table.c.org_id.in_([tenant.org_id for tenant in tenants]))
            )
        migrator_connection.commit()


@pytest.fixture
def space_client(database, space_data):
    """Exercise the real shared bearer dependency and runtime-role connection pool."""
    app = create_app(Settings(database.url("fleetops_app"), space_data[0].org_id))
    with TestClient(app) as client:
        yield client


@pytest.fixture
def space_values():
    """Produce fresh database inputs, allowing explicit adversarial overrides."""

    def values(tenant, table, **changes):
        common = {
            "id": uuid7(),
            "org_id": tenant.org_id,
            "created_by_actor_id": tenant.actor_id,
            "name": "New space",
        }
        if table is facilities:
            common["timezone"] = "America/Chicago"
        else:
            common.update(facility_id=tenant.facility_id, code="NEW", kind="BIN")
        return common | changes

    return values


@pytest.fixture
def space_dml(space_data, migrator_connection, app_connection):
    """Grant test-only UPDATE/DELETE to isolate RLS, then restore production privileges."""
    migrator_connection.exec_driver_sql(
        "GRANT UPDATE, DELETE ON fleetops.facilities, fleetops.locations TO fleetops_app"
    )
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "REVOKE UPDATE, DELETE ON fleetops.facilities, fleetops.locations FROM fleetops_app"
        )
        migrator_connection.commit()


@pytest.fixture
def space_snapshot(migrator_connection):
    """Read committed fixture state as owner only to verify runtime operations changed nothing."""

    def snapshot(table, row_id):
        row = dict(
            migrator_connection.execute(select(table).where(table.c.id == row_id)).mappings().one()
        )
        migrator_connection.rollback()
        return row

    return snapshot
