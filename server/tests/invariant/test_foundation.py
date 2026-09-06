"""Invariants every later slice inherits: PostgreSQL 16, two unprivileged roles, empty baseline."""

from conftest import assert_migration_succeeded, user_relations

from fleetops.db.grants import grant_immutable_history


def test_server_is_postgresql_16(migrator_connection):
    version = int(migrator_connection.exec_driver_sql("SHOW server_version_num").scalar_one())
    assert version // 10000 == 16


def test_roles_are_unprivileged_and_independent(migrator_connection, app_connection):
    # Failure mode guarded: privilege escalation by inheritance. A role that is a member
    # of anything, or that inherits, can hold authority no GRANT in this repository shows.
    for connection, role in (
        (migrator_connection, "fleetops_migrator"),
        (app_connection, "fleetops_app"),
    ):
        assert connection.exec_driver_sql("SELECT current_user, session_user").one() == (role, role)
        assert connection.exec_driver_sql(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        ).one() == (True, False, False, False, False, False, False)
        assert (
            connection.exec_driver_sql(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                "WHERE r.rolname = current_user"
            ).all()
            == []
        )


def test_baseline_contains_only_alembic_version(database, migrator_connection):
    # Head now contains Slice 2. Prove the historical empty baseline by actually
    # downgrading to it, then restore head; do not relabel domain tables as "empty".
    try:
        assert_migration_succeeded(database.migrate("downgrade", "0001_empty_baseline"))
        assert user_relations(migrator_connection) == [("fleetops", "alembic_version", "r")]
        assert (
            migrator_connection.exec_driver_sql(
                "SELECT version_num FROM fleetops.alembic_version"
            ).scalar_one()
            == "0001_empty_baseline"
        )
        assert (
            migrator_connection.exec_driver_sql(
                "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='fleetops'"
            ).scalar_one()
            == 0
        )
    finally:
        migrator_connection.rollback()
        assert_migration_succeeded(database.migrate("upgrade", "head"))


def test_schema_and_version_table_are_migrator_owned(migrator_connection):
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'fleetops'"
        ).scalar_one()
        == "fleetops_migrator"
    )
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid = 'fleetops.alembic_version'::regclass"
        ).scalar_one()
        == "fleetops_migrator"
    )


def test_history_helper_allows_only_select_and_insert(
    migrator_connection, app_connection, throwaway_table
):
    table, qualified = throwaway_table
    # Applied twice: the helper must be idempotent so a re-run migration cannot widen or
    # narrow the grant set depending on how many times it happened to execute.
    grant_immutable_history(migrator_connection, table)
    grant_immutable_history(migrator_connection, table)
    migrator_connection.commit()
    app_connection.exec_driver_sql(f"INSERT INTO {qualified} VALUES (2, 20)")
    assert app_connection.exec_driver_sql(f"SELECT * FROM {qualified} ORDER BY id").all() == [
        (1, 10),
        (2, 20),
    ]
    app_connection.commit()
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert app_connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, %s)", (qualified, privilege)
        ).scalar_one() is (privilege in ("SELECT", "INSERT"))
    app_connection.rollback()
