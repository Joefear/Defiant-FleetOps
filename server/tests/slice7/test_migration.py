"""Exact historical restoration, no invented witnesses, and installed PostgreSQL authority."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice4.test_space_cycle_migration import guard_objects
from server.tests.slice6.conftest import for_asset
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice7.conftest import SEQUENCE, TABLES, runtime_assignment
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from fleetops.db.metadata import asset_configurations, asset_initial_assignment_facts
from fleetops.domain.asset_facts import reconcile_assets

INSERT_COLUMNS = {
    "id",
    "org_id",
    "asset_id",
    "image_name",
    "image_version",
    "config_profile",
    "notes",
    "applied_at",
    "evidence_ref",
}


def sequence_snapshot(connection):
    """Supplement the older snapshot with identity, owned sequence, ACL and allocation state."""
    sequence = connection.exec_driver_sql("""
        SELECT c.relname, pg_get_userbyid(c.relowner), c.relacl::text,
               s.seqtypid::regtype::text, s.seqstart, s.seqincrement, s.seqmax,
               s.seqmin, s.seqcache, s.seqcycle, d.deptype,
               t.relname, a.attname, a.attidentity
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_sequence s ON s.seqrelid=c.oid
        JOIN pg_depend d ON d.classid='pg_class'::regclass AND d.objid=c.oid
             AND d.refclassid='pg_class'::regclass AND d.refobjsubid>0
        JOIN pg_class t ON t.oid=d.refobjid
        JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=d.refobjsubid
        WHERE n.nspname='fleetops' ORDER BY c.relname
    """).all()
    values = (
        connection.exec_driver_sql(f"SELECT last_value, is_called FROM {SEQUENCE}").all()
        if (sequence)
        else []
    )
    connection.rollback()
    return sequence, values


def assert_only_slice7_changes(before, upgraded):
    """Allow only the Slice 7 changes plus Slice 8/9 objects at current head."""
    assert set(upgraded) == set(before) | {table.name for table in TABLES} | {
        "purchase_orders",
        "purchase_order_lines",
        "receipts",
        "receipt_comparators",
        "receipt_lines",
        "receipt_reconciliations",
        "receiving_exceptions",
    }
    for name, definition in before.items():
        if name not in {"assets", "alembic_version", "functions", "triggers"}:
            assert upgraded[name] == definition
    assert upgraded["assets"] | {"constraints": before["assets"]["constraints"]} == before["assets"]
    removed = set(before["assets"]["constraints"]) - set(upgraded["assets"]["constraints"])
    added = set(upgraded["assets"]["constraints"]) - set(before["assets"]["constraints"])
    assert removed == {
        ("ck_assets_assignment_unavailable", "CHECK ((current_assignment_id IS NULL))")
    }
    assert added == {
        (
            "fk_assets_assignment",
            "FOREIGN KEY (org_id, id, current_assignment_id) REFERENCES "
            "fleetops.asset_assignment_events(org_id, asset_id, id)",
        )
    }
    assert set(before["functions"]) <= set(upgraded["functions"])
    assert len(upgraded["functions"]) == len(before["functions"]) + 9
    assert set(before["triggers"]) <= set(upgraded["triggers"])
    assert len(upgraded["triggers"]) == len(before["triggers"]) + 22


def test_fresh_0007_round_trip_restores_exact_schema_security_and_sequence(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            head = schema_snapshot(connection), sequence_snapshot(connection)
            roles = role_snapshot(connection)
            try:
                migrate(fresh_database, "downgrade", "0007_asset_fact_history")
                assert schema_snapshot(connection) == fresh_database.historical_0007
                assert sequence_snapshot(connection) == ([], [])
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "upgrade", "head")
                assert (schema_snapshot(connection), sequence_snapshot(connection)) == head
                assert_only_slice7_changes(fresh_database.historical_0007, head[0])
                assert role_snapshot(connection) == roles
                migrate(fresh_database, "check")
            finally:
                connection.rollback()
                migrate(fresh_database, "upgrade", "head")
    finally:
        engine.dispose()


def test_populated_0007_round_trip_never_backfills_or_lazily_invents_assignment_truth(
    database,
    space_data,
    seed_asset,
    migrator_connection,
    app_connection,
):
    connection = migrator_connection
    try:
        migrate(database, "downgrade", "0007_asset_fact_history")
        a = for_asset(space_data[0], seed_asset(space_data[0], initial_assignment=False)["id"])
        before, roles = schema_snapshot(connection), role_snapshot(connection)
        assert sequence_snapshot(connection) == ([], [])
        migrate(database, "upgrade", "head")
        upgraded, sequence = schema_snapshot(connection), sequence_snapshot(connection)
        assert_only_slice7_changes(before, upgraded)
        assert all(upgraded[table.name]["rows"] == [] for table in TABLES)
        assert role_snapshot(connection) == roles
        assert guard_objects(connection) == (1, 1, 1)
        connection.rollback()
        with app_connection.begin():
            set_authenticated(app_connection, a)
            row = reconcile_assets(app_connection)[0]
            assert row["asset_id"] == a.asset_id
            assert row["discrepancies"] == ["missing_initial_assignment_facts"]
        for unassign in (False, True):
            with pytest.raises(DBAPIError) as error:
                runtime_assignment(app_connection, a, unassign=unassign)
            assert error.value.orig.sqlstate == "P0001"
        assert schema_snapshot(connection) == upgraded
        assert sequence_snapshot(connection) == sequence
        migrate(database, "check")
        migrate(database, "downgrade", "0007_asset_fact_history")
        assert schema_snapshot(connection) == before
        assert sequence_snapshot(connection) == ([], [])
        assert role_snapshot(connection) == roles
        migrate(database, "upgrade", "head")
        assert schema_snapshot(connection) == upgraded
        assert sequence_snapshot(connection) == sequence
        assert role_snapshot(connection) == roles
        migrate(database, "check")
    finally:
        app_connection.rollback()
        connection.rollback()
        migrate(database, "upgrade", "head")


def test_active_assignment_downgrade_rejects_atomically_without_discarding_history(
    database,
    asset_data,
    app_connection,
    migrator_connection,
):
    runtime_assignment(app_connection, asset_data[0])
    before = schema_snapshot(migrator_connection), sequence_snapshot(migrator_connection)
    result = database.migrate("downgrade", "0007_asset_fact_history")
    assert result.returncode != 0
    assert "ck_assets_assignment_unavailable" in result.stdout + result.stderr
    assert (schema_snapshot(migrator_connection), sequence_snapshot(migrator_connection)) == before


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_installed_tables_have_exact_fields_defaults_rls_and_minimal_grants(
    table,
    migrator_connection,
):
    connection = migrator_connection
    # Deparse defaults with schema qualification, independent of the login search path.
    connection.exec_driver_sql("SET LOCAL search_path = pg_catalog")
    assert connection.exec_driver_sql("SHOW server_version_num").scalar_one() == "160015"
    qualified = f"fleetops.{table.name}"
    assert connection.exec_driver_sql(
        "SELECT pg_get_userbyid(relowner), relrowsecurity FROM pg_class WHERE oid=%s::regclass",
        (qualified,),
    ).one() == ("fleetops_migrator", True)
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
                dict(role=role, table=qualified, privilege=privilege),
            ).scalar_one() is (role == "fleetops_app" and privilege == "SELECT")
        for column in table.c:
            for privilege in ("INSERT", "UPDATE", "REFERENCES"):
                allowed = (
                    role == "fleetops_app"
                    and table is asset_configurations
                    and privilege == "INSERT"
                    and column.name in INSERT_COLUMNS
                )
                assert (
                    connection.execute(
                        text("SELECT has_column_privilege(:role, :table, :column, :privilege)"),
                        dict(role=role, table=qualified, column=column.name, privilege=privilege),
                    ).scalar_one()
                    is allowed
                )
    columns = {
        r.column_name: r
        for r in connection.exec_driver_sql(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity, "
            "identity_generation FROM information_schema.columns "
            "WHERE table_schema='fleetops' AND table_name=%s",
            (table.name,),
        ).mappings()
    }
    common = {"org_id", "asset_id", "recorded_at"}
    if table is asset_initial_assignment_facts:
        expected, nullable = common | {"actor_id", "occurred_at"}, set()
    elif table is asset_configurations:
        expected = common | {
            "id",
            "image_name",
            "image_version",
            "config_profile",
            "notes",
            "applied_by",
            "applied_at",
            "evidence_ref",
            "configuration_seq",
        }
        nullable = {"evidence_ref"}
        assert columns["configuration_seq"].data_type == "bigint"
        assert columns["configuration_seq"].is_identity == "YES"
        assert columns["configuration_seq"].identity_generation == "ALWAYS"
        assert columns["applied_by"].column_default == "fleetops.current_authenticated_actor()"
    else:
        nullable = {
            "from_assignee_type",
            "from_assignee_id",
            "to_assignee_type",
            "to_assignee_id",
            "corrects_assignment_event_id",
            "client_op_id",
        }
        expected = common | nullable | {"id", "actor_id", "occurred_at", "reason", "result_version"}
        assert columns["result_version"].data_type == "integer"
    assert set(columns) == expected
    assert {name for name, c in columns.items() if c.is_nullable == "YES"} == nullable
    claim = "applied_at" if table is asset_configurations else "occurred_at"
    assert (
        columns[claim].data_type == columns["recorded_at"].data_type == "timestamp with time zone"
    )
    assert columns[claim].column_default is None
    assert columns["recorded_at"].column_default == "statement_timestamp()"


def test_identity_is_global_always_bigint_cache_one_and_has_no_runtime_sequence_acl(
    migrator_connection,
):
    connection = migrator_connection
    rows, _ = sequence_snapshot(connection)
    assert len(rows) == 1
    row = rows[0]
    assert row[0:2] == ("asset_configurations_configuration_seq_seq", "fleetops_migrator")
    assert row[3:] == (
        "bigint",
        1,
        1,
        9223372036854775807,
        1,
        1,
        False,
        "i",
        "asset_configurations",
        "configuration_seq",
        "a",
    )
    for role in ("fleetops_app", "fleetops_authenticator"):
        for privilege in ("SELECT", "USAGE", "UPDATE"):
            assert (
                connection.execute(
                    text("SELECT has_sequence_privilege(:role, :sequence, :privilege)"),
                    dict(role=role, sequence=SEQUENCE, privilege=privilege),
                ).scalar_one()
                is False
            )
    assert (
        connection.exec_driver_sql("""
        SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid='fleetops.asset_configurations_configuration_seq_seq'::regclass
          AND a.grantee=0
    """).scalar_one()
        == 0
    )


@pytest.mark.parametrize("name", ["assign_asset", "unassign_asset"])
def test_installed_assignment_functions_have_credential_authority_and_exact_acl(
    name,
    migrator_connection,
):
    connection = migrator_connection
    row = (
        connection.execute(
            text("""
        SELECT p.oid, pg_get_userbyid(proowner) AS owner, prosecdef, proconfig,
               pg_get_function_identity_arguments(p.oid) AS arguments,
               pg_get_functiondef(p.oid) AS definition
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='fleetops' AND proname=:name
    """),
            dict(name=name),
        )
        .mappings()
        .one()
    )
    assert row["owner"] == "fleetops_migrator" and row["prosecdef"] is True
    assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
    assert "actor" not in row["arguments"] and "org" not in row["arguments"]
    assert row["arguments"].startswith("p_asset_id uuid, p_expected_version integer,")
    definition = row["definition"]
    assert "fleetops.current_authenticated_actor()" in definition
    assert definition.index("FOR UPDATE") < definition.index(
        "locked_asset.version <> p_expected_version"
    )
    for role, expected in (("fleetops_app", True), ("fleetops_authenticator", False)):
        assert (
            connection.execute(
                text("SELECT has_function_privilege(:role, :oid, 'EXECUTE')"),
                dict(role=role, oid=row["oid"]),
            ).scalar_one()
            is expected
        )
    assert (
        connection.execute(
            text("""
        SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) a
        WHERE p.oid=:oid AND a.grantee=0
    """),
            dict(oid=row["oid"]),
        ).scalar_one()
        == 0
    )
