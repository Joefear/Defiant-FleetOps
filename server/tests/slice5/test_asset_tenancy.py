"Adversarial tenant isolation and minimum privileges, asserted as fleetops_app."

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import FUNCTION_SIGNATURE, TABLES
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_transitions, assets
from fleetops.db.session import create_runtime_engine


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("missing", [False, True])
def test_asset_rls_reads_and_filtered_writes(table, missing, asset_data, asset_dml, app_connection):
    a, b = asset_data
    with app_connection.begin():
        if not missing:
            set_authenticated(app_connection, a)
        assert app_connection.exec_driver_sql("SELECT current_user, session_user").one() == (
            "fleetops_app",
            "fleetops_app",
        )
        visible = app_connection.execute(select(table)).mappings().all()
        assert len(visible) == (0 if missing else 1)
        assert all(row["org_id"] == a.org_id for row in visible)
        target = a if missing else b
        assert (
            app_connection.execute(select(table).where(table.c.org_id == target.org_id)).all() == []
        )
        assert (
            app_connection.execute(
                table.update().where(table.c.org_id == target.org_id).values(org_id=target.org_id)
            ).rowcount
            == 0
        )
        assert (
            app_connection.execute(table.delete().where(table.c.org_id == target.org_id)).rowcount
            == 0
        )
    with app_connection.begin():
        set_authenticated(app_connection, b)
        assert len(app_connection.execute(select(table)).all()) == 1


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("missing", [False, True])
def test_asset_rls_insert_with_check(
    table, missing, asset_data, asset_dml, app_connection, migrator_connection
):
    a, b = asset_data
    tenant = a if missing else b
    row = dict(
        migrator_connection.execute(select(table).where(table.c.org_id == tenant.org_id))
        .mappings()
        .one()
    )
    row["id"] = uuid7()
    for column in table.c:
        if column.computed is not None:
            row.pop(column.name)
    migrator_connection.rollback()
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if not missing:
                set_authenticated(app_connection, a)
            app_connection.execute(table.insert().values(row))
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_asset_rls_update_with_check(table, asset_data, asset_dml, app_connection):
    a, b = asset_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                table.update().where(table.c.org_id == a.org_id).values(org_id=b.org_id)
            )
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("rollback", [False, True])
def test_asset_context_ends_before_connection_reuse(rollback, database, asset_data):
    a, b = asset_data
    engine = create_runtime_engine(database.settings(a.org_id), pool_size=1, max_overflow=0)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            set_authenticated(connection, a)
            pid = connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one()
            for table in TABLES:
                assert connection.execute(select(table.c.org_id)).scalars().all() == [a.org_id]
            transaction.rollback() if rollback else transaction.commit()
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT pg_backend_pid()").scalar_one() == pid
            for table in TABLES:
                assert connection.execute(select(table)).all() == []
        with engine.begin() as connection:
            set_authenticated(connection, b)
            for table in TABLES:
                assert connection.execute(select(table.c.org_id)).scalars().all() == [b.org_id]
    finally:
        engine.dispose()


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_asset_minimum_grants_and_policies(
    table, asset_data, app_connection, migrator_connection, assert_denied
):
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert app_connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, %s)",
            (f"fleetops.{table.name}", privilege),
        ).scalar_one() is (privilege == "SELECT")
    mutable = (
        {"asset_tag", "description", "updated_by_actor_id", "updated_at"}
        if table is assets
        else set()
    )
    for column in table.c:
        assert app_connection.exec_driver_sql(
            "SELECT has_column_privilege(current_user, %s, %s, 'UPDATE')",
            (f"fleetops.{table.name}", column.name),
        ).scalar_one() is (column.name in mutable)
    assert migrator_connection.exec_driver_sql(
        ("SELECT pg_get_userbyid(relowner), relrowsecurity FROM pg_class WHERE oid=%s::regclass"),
        (f"fleetops.{table.name}",),
    ).one() == ("fleetops_migrator", True)
    policies = migrator_connection.exec_driver_sql(
        (
            "SELECT permissive, qual, with_check FROM pg_policies WHERE "
            "schemaname='fleetops' AND tablename=%s"
        ),
        (table.name,),
    ).all()
    assert {row.permissive for row in policies} == {"PERMISSIVE", "RESTRICTIVE"}
    assert all("fleetops.org_id" in row.qual and row.qual == row.with_check for row in policies)
    app_connection.rollback()
    assert_denied(app_connection, f"TRUNCATE fleetops.{table.name}")
    assert_denied(app_connection, f"INSERT INTO fleetops.{table.name} DEFAULT VALUES")


@pytest.mark.parametrize(
    "column",
    [
        "current_state",
        "version",
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "current_assignment_id",
        "id",
        "org_id",
        "item_id",
        "created_by_actor_id",
        "created_at",
    ],
)
def test_runtime_cannot_update_projections_or_identity(column, asset_data, app_connection):
    a, _ = asset_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                assets.update().where(assets.c.id == a.asset_id).values({column: assets.c[column]})
            )
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_runtime_history_is_immutable(operation, asset_data, app_connection):
    a, _ = asset_data
    statement = (
        asset_transitions.update().values(reason="Rewritten")
        if operation == "update"
        else asset_transitions.delete()
    ).where(asset_transitions.c.asset_id == a.asset_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "42501"


def test_transition_function_acl_and_safe_object_resolution(migrator_connection, app_connection):
    row = migrator_connection.exec_driver_sql(
        (
            "SELECT pg_get_userbyid(proowner), prosecdef, proconfig FROM pg_proc "
            "WHERE oid=%s::regprocedure"
        ),
        (FUNCTION_SIGNATURE,),
    ).one()
    assert row == ("fleetops_migrator", True, ["search_path=pg_catalog, pg_temp"])
    grants = migrator_connection.exec_driver_sql(
        "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END, "
        "acl.privilege_type FROM pg_proc p, LATERAL aclexplode(p.proacl) acl "
        "WHERE p.oid=%s::regprocedure",
        (FUNCTION_SIGNATURE,),
    ).all()
    assert set(grants) == {("fleetops_migrator", "EXECUTE"), ("fleetops_app", "EXECUTE")}
    assert app_connection.exec_driver_sql(
        "SELECT has_function_privilege(current_user, %s, 'EXECUTE')", (FUNCTION_SIGNATURE,)
    ).scalar_one()
    assert set(
        migrator_connection.exec_driver_sql(
            "SELECT proname FROM pg_proc JOIN pg_namespace n ON n.oid=pronamespace "
            "WHERE n.nspname='fleetops' AND prosecdef"
        ).scalars()
    ) == {
        "resolve_session",
        "issue_session",
        "revoke_current_session",
        "transition_asset",
        "move_asset",
        "change_custody",
        "change_ownership",
        "assign_asset",
        "unassign_asset",
        "create_received_unit",
    }
