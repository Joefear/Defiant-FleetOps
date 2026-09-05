"""Adversarial proofs: the runtime role is denied everything it was not explicitly given."""

from uuid import uuid4

import pytest

from fleetops.db.grants import grant_immutable_history


def test_app_has_no_table_privileges_until_explicit_grant(
    migrator_connection, app_connection, throwaway_table, assert_denied
):
    _, qualified = throwaway_table
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert (
            app_connection.exec_driver_sql(
                "SELECT has_table_privilege(current_user, %s, %s)", (qualified, privilege)
            ).scalar_one()
            is False
        )
    app_connection.rollback()
    for statement in (
        f"SELECT * FROM {qualified}",
        f"INSERT INTO {qualified} VALUES (2, 20)",
        f"UPDATE {qualified} SET value = 99",
        f"DELETE FROM {qualified}",
        f"TRUNCATE {qualified}",
    ):
        assert_denied(app_connection, statement)
    migrator_connection.exec_driver_sql(f"GRANT SELECT ON TABLE {qualified} TO fleetops_app")
    migrator_connection.commit()
    assert app_connection.exec_driver_sql(f"SELECT * FROM {qualified}").all() == [(1, 10)]
    app_connection.rollback()
    assert_denied(app_connection, f"INSERT INTO {qualified} VALUES (2, 20)")


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_app_cannot_modify_history(
    migrator_connection, app_connection, throwaway_table, assert_denied, operation
):
    table, qualified = throwaway_table
    grant_immutable_history(migrator_connection, table)
    migrator_connection.commit()
    statements = {
        "UPDATE": f"UPDATE {qualified} SET value = 99 WHERE id = 1",
        "DELETE": f"DELETE FROM {qualified} WHERE id = 1",
        "TRUNCATE": f"TRUNCATE {qualified}",
    }
    assert_denied(app_connection, statements[operation])
    assert migrator_connection.exec_driver_sql(f"SELECT * FROM {qualified}").all() == [(1, 10)]
    migrator_connection.rollback()


def test_history_helper_removes_preexisting_table_and_column_grants(
    migrator_connection, app_connection, throwaway_table, assert_denied
):
    # Failure mode guarded: PostgreSQL keeps column-level ACLs independent of table-level
    # ones, so a helper that only revoked at table level would leave UPDATE (value) alive.
    table, qualified = throwaway_table
    migrator_connection.exec_driver_sql(f"GRANT ALL ON TABLE {qualified} TO fleetops_app")
    migrator_connection.exec_driver_sql(f"GRANT UPDATE (value) ON {qualified} TO PUBLIC")
    migrator_connection.exec_driver_sql(f"GRANT REFERENCES (id) ON {qualified} TO fleetops_app")
    grant_immutable_history(migrator_connection, table)
    migrator_connection.commit()
    assert_denied(app_connection, f"UPDATE {qualified} SET value = 99")
    assert_denied(app_connection, f"DELETE FROM {qualified}")
    for column, privilege in (("value", "UPDATE"), ("id", "REFERENCES")):
        assert (
            app_connection.exec_driver_sql(
                "SELECT has_column_privilege(current_user, %s, %s, %s)",
                (qualified, column, privilege),
            ).scalar_one()
            is False
        )
    app_connection.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE fleetops.unauthorized (id integer)",
        "CREATE TABLE public.unauthorized (id integer)",
        "CREATE SCHEMA unauthorized",
        "CREATE TEMP TABLE unauthorized (id integer)",
    ],
)
def test_app_cannot_create_objects(app_connection, assert_denied, statement):
    # TEMP is included deliberately: it is granted to PUBLIC by default, and pg_temp
    # objects are the usual way an unprivileged role shadows names on a search path.
    assert_denied(app_connection, statement)


def test_app_cannot_assume_migrator(app_connection, assert_denied):
    assert_denied(app_connection, "SET ROLE fleetops_migrator")


def test_app_cannot_change_alembic_version(app_connection, assert_denied):
    assert_denied(app_connection, "UPDATE fleetops.alembic_version SET version_num = 'tampered'")


def test_new_sequence_requires_explicit_grant(migrator_connection, app_connection, assert_denied):
    # Sequences are not covered by table grants and have their own default posture.
    # USAGE permits nextval only; setval needs UPDATE, and rewinding a sequence is not
    # something the runtime role should ever be able to do.
    qualified = f"fleetops.slice1_sequence_{uuid4().hex}"
    migrator_connection.exec_driver_sql(f"CREATE SEQUENCE {qualified}")
    migrator_connection.commit()
    try:
        assert_denied(app_connection, f"SELECT nextval('{qualified}')")
        migrator_connection.exec_driver_sql(f"GRANT USAGE ON SEQUENCE {qualified} TO fleetops_app")
        migrator_connection.commit()
        assert app_connection.exec_driver_sql(f"SELECT nextval('{qualified}')").scalar_one() == 1
        app_connection.rollback()
        assert_denied(app_connection, f"SELECT setval('{qualified}', 50)")
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(f"DROP SEQUENCE {qualified}")
        migrator_connection.commit()
