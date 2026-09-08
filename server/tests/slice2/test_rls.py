"""USING visibility and WITH CHECK admission are distinct PostgreSQL contracts (ADR-002)."""

import pytest
from conftest import TABLES, row_snapshot
from server.tests.auth_context import set_authenticated
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.session import create_runtime_engine


def assert_rls_error(error):
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("name", sorted(TABLES))
def test_cross_org_select_returns_zero_rows(name, tenants, permitted_dml, app_connection):
    a, b = tenants
    table = TABLES[name]
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert app_connection.exec_driver_sql("SELECT current_user").scalar_one() == "fleetops_app"
        assert app_connection.execute(
            select(table.c.id).where(table.c.id == a.ids[name])
        ).all() == [(a.ids[name],)]
        assert app_connection.execute(select(table).where(table.c.id == b.ids[name])).all() == []


@pytest.mark.parametrize("name", sorted(TABLES))
def test_no_context_direct_select_returns_zero_rows(name, tenants, permitted_dml, app_connection):
    assert app_connection.execute(select(TABLES[name])).all() == []


@pytest.mark.parametrize("name", sorted(TABLES))
@pytest.mark.parametrize("missing_context", [False, True], ids=["wrong-org", "no-context"])
def test_insert_requires_matching_context(
    name,
    missing_context,
    tenants,
    permitted_dml,
    app_connection,
    migrator_connection,
):
    a, b = tenants
    table = TABLES[name]
    target = a if missing_context else b
    values = row_snapshot(migrator_connection, table, target.ids[name])
    for column in table.columns:
        if column.computed is not None:
            values.pop(column.name)
    # Root id is its scope; the other candidate ids are fresh while their org is wrong.
    if name != "organizations":
        values["id"] = uuid7()
    if not missing_context:
        app_connection.begin()
        set_authenticated(app_connection, a)
    with pytest.raises(DBAPIError) as error:
        app_connection.execute(table.insert().values(**values))
    assert_rls_error(error)
    app_connection.rollback()


@pytest.mark.parametrize("name", sorted(TABLES))
@pytest.mark.parametrize("missing_context", [False, True], ids=["cross-org", "no-context"])
def test_hidden_update_delete_affect_zero_and_preserve_row(
    name,
    missing_context,
    tenants,
    permitted_dml,
    app_connection,
    migrator_connection,
):
    a, b = tenants
    table = TABLES[name]
    target = a if missing_context else b
    original = row_snapshot(migrator_connection, table, target.ids[name])
    changes = {
        "organizations": {"name": "attempted change"},
        "actors": {"display_name": "attempted change"},
        "parties": {"display_name": "attempted change"},
        "party_roles": {"role": "VENDOR"},
        "users": {"username": "attempted-change"},
        "sessions": {"active": False},
    }
    with app_connection.begin():
        if not missing_context:
            set_authenticated(app_connection, a)
        assert (
            app_connection.execute(
                table.update().where(table.c.id == target.ids[name]).values(**changes[name])
            ).rowcount
            == 0
        )
        assert (
            app_connection.execute(table.delete().where(table.c.id == target.ids[name])).rowcount
            == 0
        )
    assert row_snapshot(migrator_connection, table, target.ids[name]) == original


@pytest.mark.parametrize("name", sorted(TABLES))
def test_visible_row_cannot_move_to_another_tenant(
    name,
    tenants,
    permitted_dml,
    app_connection,
    migrator_connection,
):
    a, b = tenants
    table = TABLES[name]
    original = row_snapshot(migrator_connection, table, a.ids[name])
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            values = {"id": b.org_id} if name == "organizations" else {"org_id": b.org_id}
            app_connection.execute(table.update().where(table.c.id == a.ids[name]).values(**values))
    assert_rls_error(error)
    assert row_snapshot(migrator_connection, table, a.ids[name]) == original


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_same_pooled_connection_has_no_tenant_after_transaction(database, tenants, rollback):
    a, b = tenants
    table = TABLES["parties"]
    engine = create_runtime_engine(
        database.settings(a.org_id),
        pool_size=1,
        max_overflow=0,
    )
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            set_authenticated(connection, a)
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            assert connection.execute(select(table.c.org_id)).scalars().all() == [a.org_id]
            transaction.rollback() if rollback else transaction.commit()
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            assert connection.execute(select(table)).all() == []
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            set_authenticated(connection, b)
            assert connection.execute(select(table.c.org_id)).scalars().all() == [b.org_id]
        with engine.begin() as connection:
            assert connection.execute(select(table)).all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("name", sorted(TABLES))
def test_runtime_has_no_truncate_privilege(name, app_connection, assert_denied):
    assert (
        app_connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, 'TRUNCATE')",
            (f"fleetops.{name}",),
        ).scalar_one()
        is False
    )
    app_connection.rollback()
    assert_denied(app_connection, f"TRUNCATE fleetops.{name}")
