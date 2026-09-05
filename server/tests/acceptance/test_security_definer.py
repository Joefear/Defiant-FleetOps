"""Slice 1 acceptance: the D3 write path. The app role reaches a projection only through
a migrator-owned SECURITY DEFINER function, never by UPDATE."""

from uuid import uuid4


def test_migrator_owned_definer_executes_without_direct_update(
    migrator_connection, app_connection, throwaway_table, assert_denied
):
    # The function pins its own search_path so a caller cannot redirect the qualified
    # names inside it; the catalog assertion below checks that pin was recorded.
    _, qualified = throwaway_table
    function = f"fleetops.slice1_increment_{uuid4().hex}"
    migrator_connection.exec_driver_sql(f"""
        CREATE FUNCTION {function}() RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, fleetops, pg_temp
        AS $$
        DECLARE updated_value integer;
        BEGIN
            UPDATE {qualified} SET value = value + 1 WHERE id = 1
                RETURNING value INTO updated_value;
            RETURN updated_value;
        END;
        $$
    """)
    migrator_connection.commit()
    try:
        assert migrator_connection.exec_driver_sql(
            "SELECT pg_get_userbyid(proowner), prosecdef, proconfig "
            "FROM pg_proc WHERE oid = %s::regprocedure",
            (function + "()",),
        ).one() == ("fleetops_migrator", True, ["search_path=pg_catalog, fleetops, pg_temp"])
        migrator_connection.rollback()
        assert_denied(app_connection, f"SELECT {function}()")
        assert_denied(app_connection, f"UPDATE {qualified} SET value = 99 WHERE id = 1")
        migrator_connection.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION {function}() TO fleetops_app"
        )
        migrator_connection.commit()
        # The definer's UPDATE runs inside the caller's transaction: rolled back, the
        # migrator still sees 10; committed, it sees 11. Atomicity with the caller is what
        # lets a later transition write history and projection as one unit (D3).
        assert app_connection.exec_driver_sql(f"SELECT {function}()").scalar_one() == 11
        app_connection.rollback()
        assert (
            migrator_connection.exec_driver_sql(f"SELECT value FROM {qualified}").scalar_one() == 10
        )
        migrator_connection.rollback()
        assert app_connection.exec_driver_sql(f"SELECT {function}()").scalar_one() == 11
        app_connection.commit()
        assert (
            migrator_connection.exec_driver_sql(f"SELECT value FROM {qualified}").scalar_one() == 11
        )
        migrator_connection.rollback()
        assert_denied(app_connection, f"UPDATE {qualified} SET value = 99 WHERE id = 1")
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(f"DROP FUNCTION {function}()")
        migrator_connection.commit()
