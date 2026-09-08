"""Independent tenants with runtime-generated credentials on the real migrated database."""

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.auth import hash_password, token_digest
from fleetops.db.metadata import (
    actors,
    metadata,
    organizations,
    parties,
    party_roles,
    sessions,
    users,
)
from fleetops.db.tenancy import RUNTIME_GRANTS, apply_tenant_policy

# Keep the Slice 2 proof set fixed as later slices extend shared runtime metadata.
TABLES = {
    table.name: table for table in (organizations, actors, parties, party_roles, users, sessions)
}


@dataclass(frozen=True)
class Tenant:
    org_id: UUID
    ids: dict[str, UUID]
    username: str
    password: str = field(repr=False)
    raw_token: str = field(repr=False)


@pytest.fixture
def tenants(migrator_connection, app_connection):
    """Populate two real tenants; setup authority is not the identity used for assertions."""
    result = []
    try:
        for label in ("A", "B"):
            ids = {name: uuid7() for name in TABLES}
            tenant = Tenant(
                ids["organizations"],
                ids,
                "operator",
                secrets.token_urlsafe(24),
                secrets.token_urlsafe(32),
            )
            migrator_connection.execute(
                organizations.insert().values(id=tenant.org_id, name=f"Tenant {label}")
            )
            migrator_connection.execute(
                actors.insert().values(
                    id=ids["actors"],
                    org_id=tenant.org_id,
                    type="HUMAN",
                    display_name=f"Human {label}",
                    created_by_actor_id=ids["actors"],
                )
            )
            migrator_connection.execute(
                parties.insert().values(
                    id=ids["parties"],
                    org_id=tenant.org_id,
                    display_name=f"Party {label}",
                    created_by_actor_id=ids["actors"],
                )
            )
            migrator_connection.execute(
                party_roles.insert().values(
                    id=ids["party_roles"],
                    org_id=tenant.org_id,
                    party_id=ids["parties"],
                    role="INTERNAL",
                )
            )
            migrator_connection.execute(
                users.insert().values(
                    id=ids["users"],
                    org_id=tenant.org_id,
                    actor_id=ids["actors"],
                    username=tenant.username,
                    password_hash=hash_password(tenant.password),
                )
            )
            migrator_connection.execute(
                sessions.insert().values(
                    id=ids["sessions"],
                    org_id=tenant.org_id,
                    user_id=ids["users"],
                    token_digest=token_digest(tenant.raw_token),
                    created_at=datetime.now(UTC) - timedelta(days=1),
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                )
            )
            result.append(tenant)
        migrator_connection.commit()
        yield tuple(result)
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for table in reversed(metadata.sorted_tables):
            migrator_connection.execute(
                table.delete().where(table.c.org_id.in_([tenant.org_id for tenant in result]))
            )
        migrator_connection.commit()


@pytest.fixture
def permitted_dml(tenants, migrator_connection, app_connection):
    """Isolate RLS from narrower production grants without broadening runtime permissions.

    Some tables intentionally have no runtime UPDATE/DELETE path. Grant those operations
    only during this adversarial test so a denial cannot pass merely on a missing grant.
    Restore the exact production policy/grant pattern in teardown.
    """
    for name in TABLES:
        migrator_connection.exec_driver_sql(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON fleetops.{name} TO fleetops_app"
        )
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for name, table in TABLES.items():
            apply_tenant_policy(migrator_connection, table, privileges=RUNTIME_GRANTS[name])
        migrator_connection.commit()


@pytest.fixture
def client(database, tenants):
    app = create_app(database.settings(tenants[0].org_id))
    with TestClient(app) as client:
        yield client


def row_snapshot(connection, table, row_id):
    row = dict(connection.execute(select(table).where(table.c.id == row_id)).mappings().one())
    connection.rollback()
    return row
