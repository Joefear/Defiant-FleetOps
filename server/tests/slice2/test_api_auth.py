"""HTTP proof: untrusted input never supplies tenancy or human attribution."""

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from conftest import TABLES
from sqlalchemy import select

from fleetops.auth import PASSWORD_HASHER
from fleetops.db.metadata import actors, parties, sessions, users


def snapshot(connection):
    rows = {
        name: connection.execute(select(table).order_by(table.c.id)).all()
        for name, table in TABLES.items()
    }
    connection.rollback()
    return rows


def bearer(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def test_password_login_issues_random_token_and_stores_only_sha256(
    client,
    tenants,
    migrator_connection,
    monkeypatch,
):
    a, _ = tenants
    stored = migrator_connection.execute(
        select(users.c.password_hash).where(users.c.id == a.ids["users"])
    ).scalar_one()
    migrator_connection.rollback()
    assert stored.startswith("$argon2id$")
    assert a.password not in stored
    assert PASSWORD_HASHER.verify(stored, a.password)
    draws = []
    real_token_bytes = secrets.token_bytes

    def observed_entropy(nbytes=None):
        draws.append(nbytes)
        return real_token_bytes(nbytes)

    # Observe the CSPRNG input size as well as the decoded output; string length alone
    # cannot prove a generator didn't pad a weak token to look longer.
    monkeypatch.setattr("fleetops.auth.secrets.token_bytes", observed_entropy)
    response = client.post("/auth/login", json={"username": a.username, "password": a.password})
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.json()
    assert set(payload) == {"access_token", "token_type", "expires_at"}
    raw = payload["access_token"]
    assert 32 in draws and all(n is not None and n >= 32 for n in draws)
    assert len(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) >= 32
    digest = hashlib.sha256(raw.encode()).digest()
    row = (
        migrator_connection.execute(select(sessions).where(sessions.c.token_digest == digest))
        .mappings()
        .one()
    )
    assert row["org_id"] == a.org_id
    assert row["user_id"] == a.ids["users"]
    assert row["id"].version == 7
    assert raw.encode() != row["token_digest"]
    assert "token" not in sessions.c and "raw_token" not in sessions.c
    assert row["expires_at"] > datetime.now(UTC)
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {raw}"})
    assert me.status_code == 200
    assert me.json() == {
        "org_id": str(a.org_id),
        "user_id": str(a.ids["users"]),
        "actor_id": str(a.ids["actors"]),
    }


@pytest.mark.parametrize(
    "condition", ["wrong-password", "unknown-user", "inactive-user", "inactive-actor"]
)
def test_failed_login_returns_401_and_changes_nothing(
    condition,
    client,
    tenants,
    migrator_connection,
):
    a, _ = tenants
    username, password = a.username, a.password
    if condition == "wrong-password":
        password = secrets.token_urlsafe(24)
    elif condition == "unknown-user":
        username = "unknown-" + secrets.token_hex(6)
    else:
        table = users if condition == "inactive-user" else actors
        migrator_connection.execute(
            table.update().where(table.c.id == a.ids[table.name]).values(active=False)
        )
        migrator_connection.commit()
    before = snapshot(migrator_connection)
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert snapshot(migrator_connection) == before


@pytest.mark.parametrize(
    "path,body",
    [
        ("/actors", {"type": "DEVICE", "display_name": "unauthenticated"}),
        ("/parties", {"display_name": "unauthenticated", "roles": ["VENDOR"]}),
        ("/auth/logout", None),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Basic invalid", "Bearer invalid"])
def test_every_other_write_requires_session_and_writes_nothing(
    path,
    body,
    authorization,
    client,
    migrator_connection,
):
    before = snapshot(migrator_connection)
    response = client.post(
        path,
        json=body,
        headers={} if authorization is None else {"Authorization": authorization},
    )
    assert response.status_code == 401
    assert snapshot(migrator_connection) == before


@pytest.mark.parametrize(
    "condition", ["expired", "inactive-session", "inactive-user", "inactive-actor"]
)
def test_invalid_bearer_cannot_perform_http_write(condition, client, tenants, migrator_connection):
    a, _ = tenants
    table, values = {
        "expired": (sessions, {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}),
        "inactive-session": (sessions, {"active": False}),
        "inactive-user": (users, {"active": False}),
        "inactive-actor": (actors, {"active": False}),
    }[condition]
    migrator_connection.execute(
        table.update().where(table.c.id == a.ids[table.name]).values(**values)
    )
    migrator_connection.commit()
    before = snapshot(migrator_connection)
    response = client.post(
        "/actors",
        headers=bearer(a),
        json={"type": "DEVICE", "display_name": "denied"},
    )
    assert response.status_code == 401
    assert snapshot(migrator_connection) == before


@pytest.mark.parametrize("actor_type", ["HUMAN", "SYSTEM", "DEVICE", "INTEGRATION"])
def test_actor_creation_uses_session_attribution_and_uuid7(
    actor_type,
    client,
    tenants,
    migrator_connection,
):
    a, _ = tenants
    response = client.post(
        "/actors",
        headers=bearer(a),
        json={"type": actor_type, "display_name": "New identity"},
    )
    assert response.status_code == 201
    created = response.json()
    assert UUID(created["id"]).version == 7
    assert created["org_id"] == str(a.org_id)
    assert created["created_by_actor_id"] == str(a.ids["actors"])
    assert created["type"] == actor_type
    assert created["active"] is True
    assert (
        migrator_connection.execute(
            select(users.c.id).where(users.c.actor_id == UUID(created["id"]))
        ).all()
        == []
    )


def test_party_can_hold_multiple_roles_once(client, tenants):
    a, _ = tenants
    response = client.post(
        "/parties",
        headers=bearer(a),
        json={"display_name": "Combined role", "roles": ["VENDOR", "MANUFACTURER", "VENDOR"]},
    )
    assert response.status_code == 201
    party = response.json()
    assert UUID(party["id"]).version == 7
    assert party["roles"] == ["MANUFACTURER", "VENDOR"]
    assert party["created_by_actor_id"] == str(a.ids["actors"])
    listing = client.get("/parties", headers=bearer(a))
    assert listing.status_code == 200
    assert party in listing.json()


def test_request_headers_and_query_cannot_override_identity(client, tenants, migrator_connection):
    a, b = tenants
    response = client.post(
        "/parties",
        headers={**bearer(a), "X-Org-ID": str(b.org_id), "X-Actor-ID": str(b.ids["actors"])},
        params={"org_id": str(b.org_id), "actor_id": str(b.ids["actors"])},
        json={"display_name": "Attributed correctly", "roles": ["INTERNAL"]},
    )
    assert response.status_code == 201
    party = response.json()
    assert party["org_id"] == str(a.org_id)
    assert party["created_by_actor_id"] == str(a.ids["actors"])
    stored = migrator_connection.execute(
        select(parties.c.org_id, parties.c.created_by_actor_id).where(
            parties.c.id == UUID(party["id"])
        )
    ).one()
    assert stored == (a.org_id, a.ids["actors"])
    # A valid B credential, unlike arbitrary headers, resolves to B even though the
    # server's login configuration is A. The bearer, not a default org, is authority.
    for tenant in (a, b):
        result = client.get("/actors", headers=bearer(tenant), params={"org_id": str(b.org_id)})
        assert result.status_code == 200
        assert all(row["org_id"] == str(tenant.org_id) for row in result.json())


@pytest.mark.parametrize("field", ["org_id", "actor_id", "created_by_actor_id"])
def test_body_cannot_supply_org_or_performer(field, client, tenants, migrator_connection):
    a, b = tenants
    before = snapshot(migrator_connection)
    response = client.post(
        "/parties",
        headers=bearer(a),
        json={"display_name": "spoof", "roles": ["VENDOR"], field: str(b.ids["actors"])},
    )
    assert response.status_code == 422
    assert snapshot(migrator_connection) == before


def test_login_uses_only_configured_org_and_does_not_offer_a_selector(
    client,
    tenants,
    migrator_connection,
):
    a, b = tenants
    before = snapshot(migrator_connection)
    response = client.post(
        "/auth/login",
        headers={"X-Org-ID": str(b.org_id)},
        params={"org_id": str(b.org_id)},
        json={"username": b.username, "password": b.password},
    )
    assert response.status_code == 401
    assert snapshot(migrator_connection) == before
    response = client.post(
        "/auth/login",
        json={"username": a.username, "password": a.password, "org_id": str(b.org_id)},
    )
    assert response.status_code == 422
    assert snapshot(migrator_connection) == before


def test_logout_revokes_only_presented_session(client, tenants, migrator_connection):
    a, b = tenants
    assert client.post("/auth/logout", headers=bearer(a)).status_code == 204
    assert client.get("/auth/me", headers=bearer(a)).status_code == 401
    assert client.get("/auth/me", headers=bearer(b)).status_code == 200
    assert (
        migrator_connection.execute(
            select(sessions.c.active).where(sessions.c.id == a.ids["sessions"])
        ).scalar_one()
        is False
    )
    assert (
        migrator_connection.execute(
            select(sessions.c.active).where(sessions.c.id == b.ids["sessions"])
        ).scalar_one()
        is True
    )


def test_http_scope_has_no_later_slice_or_user_management_routes(client):
    methods = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if route.path
        not in (
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
        )
    }
    # Keep an exact current-head allowlist: only the eight authorized Slice 3 routes
    # extend the original surface. User management and later workflows remain excluded.
    assert methods == {
        ("POST", "/auth/login"),
        ("POST", "/auth/logout"),
        ("GET", "/auth/me"),
        ("POST", "/actors"),
        ("GET", "/actors"),
        ("POST", "/parties"),
        ("GET", "/parties"),
        ("POST", "/items"),
        ("GET", "/items"),
        ("GET", "/items/{item_id}"),
        ("PATCH", "/items/{item_id}"),
        ("DELETE", "/items/{item_id}"),
        ("POST", "/external-references"),
        ("GET", "/external-references"),
        ("GET", "/external-references/search"),
    }
