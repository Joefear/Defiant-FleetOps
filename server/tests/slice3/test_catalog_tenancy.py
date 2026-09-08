"""RLS proofs execute ordinary statements as fleetops_app with populated A/B targets."""

import pytest
from server.tests.auth_context import set_authenticated
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import external_references, items
from fleetops.db.session import create_runtime_engine

TABLES = [items, external_references]


def target_id(tenant, table):
    return tenant.item_id if table is items else tenant.reference_id


def assert_rls(error):
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("missing", [False, True], ids=["cross-org", "no-context"])
def test_catalog_rls_select_and_hidden_writes(
    table,
    missing,
    catalog_data,
    catalog_dml,
    app_connection,
    catalog_snapshot,
):
    a, b = catalog_data
    target = a if missing else b
    row_id = target_id(target, table)
    before = catalog_snapshot(table, row_id)
    with app_connection.begin():
        if not missing:
            set_authenticated(app_connection, a)
        assert app_connection.exec_driver_sql("SELECT current_user, session_user").one() == (
            "fleetops_app",
            "fleetops_app",
        )
        visible = app_connection.execute(select(table.c.id)).scalars().all()
        assert visible == ([] if missing else [target_id(a, table)])
        assert app_connection.execute(select(table).where(table.c.id == row_id)).all() == []
        changes = (
            {"description": "attempted"} if table is items else {"external_value": "attempted"}
        )
        assert (
            app_connection.execute(
                table.update().where(table.c.id == row_id).values(**changes)
            ).rowcount
            == 0
        )
        assert app_connection.execute(table.delete().where(table.c.id == row_id)).rowcount == 0
    assert catalog_snapshot(table, row_id) == before


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("missing", [False, True], ids=["wrong-org", "no-context"])
def test_catalog_rls_insert_requires_current_organization(
    table,
    missing,
    catalog_data,
    app_connection,
    catalog_snapshot,
):
    a, b = catalog_data
    target = a if missing else b
    values = catalog_snapshot(table, target_id(target, table))
    values["id"] = uuid7()
    for column in table.columns:
        if column.computed is not None:
            values.pop(column.name)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if not missing:
                set_authenticated(app_connection, a)
            app_connection.execute(table.insert().values(**values))
    assert_rls(error)


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_catalog_rls_visible_row_cannot_move_to_other_organization(
    table,
    catalog_data,
    catalog_dml,
    app_connection,
    catalog_snapshot,
):
    a, b = catalog_data
    row_id = target_id(a, table)
    before = catalog_snapshot(table, row_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                table.update().where(table.c.id == row_id).values(org_id=b.org_id)
            )
    assert_rls(error)
    assert catalog_snapshot(table, row_id) == before


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
def test_catalog_tenant_context_does_not_survive_pool_reuse(database, catalog_data, rollback):
    a, b = catalog_data
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
            for table in TABLES:
                assert connection.execute(select(table.c.id)).scalars().all() == [
                    target_id(a, table)
                ]
            transaction.rollback() if rollback else transaction.commit()
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            for table in TABLES:
                assert connection.execute(select(table)).all() == []
        with engine.begin() as connection:
            set_authenticated(connection, b)
            for table in TABLES:
                assert connection.execute(select(table.c.id)).scalars().all() == [
                    target_id(b, table)
                ]
        with engine.begin() as connection:
            for table in TABLES:
                assert connection.execute(select(table)).all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_catalog_runtime_grants_are_minimal_and_truncate_is_denied(
    table,
    catalog_data,
    app_connection,
    migrator_connection,
    assert_denied,
):
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert app_connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, %s)",
            (f"fleetops.{table.name}", privilege),
        ).scalar_one() is (privilege in {"SELECT", "INSERT"})
    mutable = (
        {
            "manufacturer_party_id",
            "manufacturer_part_number",
            "revision",
            "description",
            "uom",
            "serialized",
            "export_classification",
            "export_controlled",
            "active",
            "updated_by_actor_id",
            "updated_at",
        }
        if table is items
        else set()
    )
    for column in table.c:
        assert app_connection.exec_driver_sql(
            "SELECT has_column_privilege(current_user, %s, %s, 'UPDATE')",
            (f"fleetops.{table.name}", column.name),
        ).scalar_one() is (column.name in mutable)
    assert migrator_connection.exec_driver_sql(
        "SELECT pg_get_userbyid(relowner), relrowsecurity FROM pg_class WHERE oid=%s::regclass",
        (f"fleetops.{table.name}",),
    ).one() == ("fleetops_migrator", True)
    policies = migrator_connection.exec_driver_sql(
        "SELECT permissive, qual, with_check FROM pg_policies "
        "WHERE schemaname='fleetops' AND tablename=%s",
        (table.name,),
    ).all()
    assert {row.permissive for row in policies} == {"PERMISSIVE", "RESTRICTIVE"}
    assert all("fleetops.org_id" in row.qual and row.qual == row.with_check for row in policies)
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) acl "
            "WHERE c.oid=%s::regclass AND acl.grantee=0",
            (f"fleetops.{table.name}",),
        ).scalar_one()
        == 0
    )
    app_connection.rollback()
    assert_denied(app_connection, f"TRUNCATE fleetops.{table.name}")
