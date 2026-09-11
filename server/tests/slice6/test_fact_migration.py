"""Exact PostgreSQL migration boundaries, no-backfill evidence and physical-fact schema."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice4.test_space_cycle_migration import guard_objects
from server.tests.slice6.conftest import (
    KINDS,
    OWNERSHIP,
    TABLES,
    baseline_values,
    for_asset,
    runtime_fact,
)
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from fleetops.db.metadata import asset_initial_facts, assets
from fleetops.domain.asset_facts import reconcile_assets


def migrate(database, *arguments):
    result = database.migrate(*arguments)
    assert result.returncode == 0, result.stdout + result.stderr


def role_snapshot(connection):
    """Bootstrap-owned identity and deny defaults must survive the new migration too."""
    result = (
        connection.exec_driver_sql("""
            SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
                   rolreplication, rolbypassrls, rolconfig FROM pg_roles
            WHERE rolname IN ('fleetops_migrator','fleetops_app','fleetops_authenticator')
            ORDER BY rolname
        """).all(),
        connection.exec_driver_sql("""
            SELECT pg_get_userbyid(roleid), pg_get_userbyid(member), admin_option,
                   inherit_option, set_option FROM pg_auth_members ORDER BY 1, 2
        """).all(),
        connection.exec_driver_sql("""
            SELECT pg_get_userbyid(defaclrole), defaclnamespace::regnamespace::text,
                   defaclobjtype, defaclacl::text FROM pg_default_acl ORDER BY 1, 2, 3
        """).all(),
    )
    connection.rollback()
    return result


def test_fresh_0006_snapshot_predates_0007_and_is_restored_exactly(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            head, roles = schema_snapshot(connection), role_snapshot(connection)
            try:
                migrate(fresh_database, "downgrade", "0006_assets")
                assert schema_snapshot(connection) == fresh_database.historical_0006
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "upgrade", "head")
                assert schema_snapshot(connection) == head
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "check")
            finally:
                connection.rollback()
                migrate(fresh_database, "upgrade", "head")
    finally:
        engine.dispose()


def test_populated_0006_round_trip_never_backfills_or_lazily_invents_baseline(
    database,
    space_data,
    seed_asset,
    migrator_connection,
    app_connection,
):
    connection = migrator_connection
    try:
        migrate(database, "downgrade", "0006_assets")
        a = for_asset(
            space_data[0],
            seed_asset(space_data[0], initial_facts=False, initial_assignment=False)["id"],
        )
        before, roles = schema_snapshot(connection), role_snapshot(connection)
        migrate(database, "upgrade", "head")
        upgraded = schema_snapshot(connection)
        for name, definition in before.items():
            if name not in {"alembic_version", "functions", "triggers", "assets"}:
                assert upgraded[name] == definition
        # Slice 7 replaces precisely the deferred NULL-only assignment constraint
        # with the activated same-tenant/same-Asset event FK. Every other detail stays exact.
        assert (
            upgraded["assets"] | {"constraints": before["assets"]["constraints"]}
            == before["assets"]
        )
        assert [c for c in upgraded["assets"]["constraints"] if c[0] != "fk_assets_assignment"] == [
            c for c in before["assets"]["constraints"] if c[0] != "ck_assets_assignment_unavailable"
        ]
        assert set(before["functions"]) <= set(upgraded["functions"])
        assert set(before["triggers"]) <= set(upgraded["triggers"])
        assert len(upgraded["functions"]) == len(before["functions"]) + 7
        assert len(upgraded["triggers"]) == len(before["triggers"]) + 13
        assert role_snapshot(connection) == roles
        assert guard_objects(connection) == (1, 1, 1)
        connection.rollback()
        assert all(upgraded[table.name]["rows"] == [] for table in TABLES)
        with app_connection.begin():
            set_authenticated(app_connection, a)
            row = reconcile_assets(app_connection)[0]
            assert row["asset_id"] == a.asset_id
            assert row["discrepancies"] == [
                "missing_initial_facts",
                "missing_initial_assignment_facts",
            ]
        for kind in KINDS:
            with pytest.raises(DBAPIError) as error:
                runtime_fact(app_connection, a, kind)
            assert error.value.orig.sqlstate == "P0001"
        assert schema_snapshot(connection) == upgraded
        migrate(database, "check")
        migrate(database, "downgrade", "0006_assets")
        assert schema_snapshot(connection) == before
        assert role_snapshot(connection) == roles
        migrate(database, "upgrade", "head")
        assert schema_snapshot(connection) == upgraded
        assert role_snapshot(connection) == roles
        migrate(database, "check")
    finally:
        app_connection.rollback()
        connection.rollback()
        migrate(database, "upgrade", "head")


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_new_tables_have_exact_schema_tenant_policies_and_read_only_runtime_grants(
    table,
    migrator_connection,
):
    connection = migrator_connection
    assert connection.exec_driver_sql("SHOW server_version_num").scalar_one() == "160015"
    qualified = f"fleetops.{table.name}"
    owner, rls = connection.exec_driver_sql(
        "SELECT pg_get_userbyid(relowner), relrowsecurity FROM pg_class WHERE oid=%s::regclass",
        (qualified,),
    ).one()
    assert owner == "fleetops_migrator" and rls
    policies = connection.exec_driver_sql(
        "SELECT policyname, permissive, roles, cmd, qual, with_check FROM pg_policies "
        "WHERE schemaname='fleetops' AND tablename=%s ORDER BY policyname",
        (table.name,),
    ).all()
    assert [(p.policyname, p.permissive) for p in policies] == [
        ("tenant_access", "PERMISSIVE"),
        ("tenant_boundary", "RESTRICTIVE"),
    ]
    for policy in policies:
        assert policy.roles == ["fleetops_app"] and policy.cmd == "ALL"
        assert policy.qual == policy.with_check
        assert "org_id" in policy.qual and "NULLIF" in policy.qual
    for role in ("fleetops_app", "fleetops_authenticator"):
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            assert connection.execute(
                text("SELECT has_table_privilege(:role, :table, :privilege)"),
                {"role": role, "table": qualified, "privilege": privilege},
            ).scalar_one() is (role == "fleetops_app" and privilege == "SELECT")
        for column in table.c:
            for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                assert (
                    connection.execute(
                        text("SELECT has_column_privilege(:role, :table, :column, :privilege)"),
                        {
                            "role": role,
                            "table": qualified,
                            "column": column.name,
                            "privilege": privilege,
                        },
                    ).scalar_one()
                    is False
                )
    columns = {
        row.column_name: row
        for row in connection.exec_driver_sql(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='fleetops' AND table_name=%s",
            (table.name,),
        ).mappings()
    }
    common = {"org_id", "asset_id", "actor_id", "occurred_at", "recorded_at"}
    if table is asset_initial_facts:
        assert set(columns) == common | {
            "initial_owner_party_id",
            "initial_custodian_party_id",
            "initial_location_id",
        }
        nullable = {"initial_custodian_party_id", "initial_location_id"}
    else:
        kind = next(k for k in KINDS if k.table is table)
        assert set(columns) == common | {
            "id",
            "result_version",
            "reason",
            "client_op_id",
            kind.from_field,
            kind.to_field,
            kind.correction,
        }
        nullable = {kind.correction, "client_op_id"}
        if kind is not OWNERSHIP:
            nullable |= {kind.from_field, kind.to_field}
        assert columns["result_version"].data_type == "integer"
    assert {name for name, col in columns.items() if col.is_nullable == "YES"} == nullable
    assert (
        columns["occurred_at"].data_type
        == columns["recorded_at"].data_type
        == "timestamp with time zone"
    )
    assert columns["recorded_at"].column_default == "statement_timestamp()"
    assert columns["occurred_at"].column_default is None


@pytest.mark.parametrize(
    "field", ["initial_owner_party_id", "actor_id", "occurred_at", "recorded_at"]
)
def test_baseline_required_facts_cannot_be_null(field, asset_data, seed_asset, migrator_connection):
    a = asset_data[0]
    target = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                asset_initial_facts.insert().values(baseline_values(target, **{field: None}))
            )
    assert error.value.orig.sqlstate == "23502"


def test_baseline_identity_enforces_one_row_and_insert_does_not_produce_a_version(
    asset_data,
    seed_asset,
    migrator_connection,
):
    a = asset_data[0]
    target = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    before = dict(
        migrator_connection.execute(select(assets).where(assets.c.id == target.asset_id))
        .mappings()
        .one()
    )
    start = migrator_connection.exec_driver_sql("SELECT statement_timestamp()").scalar_one()
    baseline = (
        migrator_connection.execute(
            asset_initial_facts.insert()
            .values(baseline_values(target))
            .returning(asset_initial_facts)
        )
        .mappings()
        .one()
    )
    end = migrator_connection.exec_driver_sql("SELECT statement_timestamp()").scalar_one()
    assert start <= baseline["recorded_at"] <= end
    assert baseline["actor_id"] == a.actor_id and "result_version" not in baseline
    assert (
        dict(
            migrator_connection.execute(select(assets).where(assets.c.id == target.asset_id))
            .mappings()
            .one()
        )
        == before
    )
    assert before["version"] == 1
    migrator_connection.commit()
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                asset_initial_facts.insert().values(baseline_values(target))
            )
    assert error.value.orig.sqlstate == "23505"
