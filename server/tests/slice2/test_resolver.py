"""Prove the context-free credential boundary as fleetops_app, not as its owner."""

import secrets
from datetime import UTC, datetime, timedelta

import pytest
from conftest import TABLES, row_snapshot
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from fleetops.auth import token_digest
from fleetops.db.metadata import actors, parties, sessions, users


def resolve(connection, digest):
    return connection.execute(
        text("SELECT * FROM fleetops.resolve_session(:digest)"),
        {"digest": digest},
    )


def test_valid_digest_returns_only_trusted_tuple_without_mutation(
    tenants,
    app_connection,
    migrator_connection,
):
    a, b = tenants
    before = {
        (name, tenant.org_id): row_snapshot(migrator_connection, table, tenant.ids[name])
        for name, table in TABLES.items()
        for tenant in (a, b)
    }
    for tenant in (a, b):
        result = resolve(app_connection, token_digest(tenant.raw_token))
        assert list(result.keys()) == ["org_id", "user_id", "actor_id"]
        assert result.all() == [(tenant.org_id, tenant.ids["users"], tenant.ids["actors"])]
    app_connection.commit()
    after = {
        (name, tenant.org_id): row_snapshot(migrator_connection, table, tenant.ids[name])
        for name, table in TABLES.items()
        for tenant in (a, b)
    }
    assert before == after
    # Resolution did not manufacture tenant context for ordinary direct table access.
    assert app_connection.execute(select(parties)).all() == []
    app_connection.rollback()
    # ADR-006 removes credential table visibility altogether, a stronger boundary.
    for table in (users, sessions):
        with pytest.raises(DBAPIError) as error:
            app_connection.execute(select(table))
        assert error.value.orig.sqlstate == "42501"
        app_connection.rollback()


@pytest.mark.parametrize(
    "condition",
    ["unknown", "expired", "inactive-session", "inactive-user", "inactive-actor"],
)
def test_each_invalid_credential_condition_returns_no_row(
    condition,
    tenants,
    app_connection,
    migrator_connection,
):
    a, _ = tenants
    digest = token_digest(a.raw_token)
    if condition == "unknown":
        digest = secrets.token_bytes(32)
    else:
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
    assert resolve(app_connection, digest).all() == []


def test_resolver_cannot_enumerate_by_ids_prefixes_or_alternate_arguments(tenants, app_connection):
    a, b = tenants
    for digest in (
        None,
        b"",
        a.ids["users"].bytes,
        b.ids["sessions"].bytes,
        token_digest(a.raw_token)[:-1],
        b"%" * 32,
        secrets.token_bytes(32),
    ):
        assert resolve(app_connection, digest).all() == []
    app_connection.rollback()
    with pytest.raises(DBAPIError) as error:
        app_connection.execute(
            text("SELECT * FROM fleetops.resolve_session(:digest, :org)"),
            {"digest": token_digest(a.raw_token), "org": b.org_id},
        )
    assert error.value.orig.sqlstate == "42883"
    app_connection.rollback()


def test_resolver_definer_signature_search_path_and_execute_acl(
    migrator_connection, app_connection
):
    row = migrator_connection.exec_driver_sql(
        "SELECT pg_get_userbyid(proowner), prosecdef, provolatile, proisstrict, "
        "proconfig, oidvectortypes(proargtypes), pg_get_function_result(oid) "
        "FROM pg_proc WHERE oid='fleetops.resolve_session(bytea)'::regprocedure"
    ).one()
    assert tuple(row) == (
        "fleetops_migrator",
        True,
        "s",
        True,
        ["search_path=pg_catalog, pg_temp"],
        "bytea",
        "TABLE(org_id uuid, user_id uuid, actor_id uuid)",
    )
    privileges = migrator_connection.exec_driver_sql(
        "SELECT acl.grantee, acl.privilege_type FROM pg_proc p, "
        "LATERAL aclexplode(p.proacl) acl "
        "WHERE p.oid='fleetops.resolve_session(bytea)'::regprocedure"
    ).all()
    app_oid, owner_oid = migrator_connection.exec_driver_sql(
        "SELECT 'fleetops_app'::regrole::oid, 'fleetops_migrator'::regrole::oid"
    ).one()
    assert set(privileges) == {(app_oid, "EXECUTE"), (owner_oid, "EXECUTE")}
    assert all(grantee != 0 for grantee, _ in privileges), "PUBLIC must not execute"
    assert (
        app_connection.exec_driver_sql(
            "SELECT has_function_privilege(current_user, "
            "'fleetops.resolve_session(bytea)', 'EXECUTE')"
        ).scalar_one()
        is True
    )


def test_runtime_cannot_turn_off_rls_to_enumerate_credentials(tenants, app_connection):
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            app_connection.exec_driver_sql("SET LOCAL row_security = off")
            app_connection.execute(select(users))
    assert error.value.orig.sqlstate == "42501"
