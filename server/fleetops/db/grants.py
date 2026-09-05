"""Explicit grant helpers for future migrations; no domain data in Slice 1."""

from sqlalchemy import Connection, text


def grant_immutable_history(
    connection: Connection, table: str, *, schema: str = "fleetops"
) -> None:
    """Allow only SELECT/INSERT on a migrator-owned history table in the caller's transaction.

    Revoke prior table AND column grants before applying the pattern. No sequence,
    schema, function, UPDATE, DELETE, TRUNCATE, REFERENCES, or TRIGGER grants are added.
    Future migrations must also define their required org/actor/time columns.
    """
    row = (
        connection.execute(
            text(
                "SELECT pg_get_userbyid(c.relowner) AS owner, current_user AS actor "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table AND c.relkind IN ('r', 'p')"
            ),
            {"schema": schema, "table": table},
        )
        .mappings()
        .one()
    )
    # A non-owner can only revoke privileges it granted itself, so "REVOKE ALL" issued by
    # anyone else would quietly leave the owner's grants in place while reporting success.
    # Refuse rather than pretend. History gets written once; anyone who wants to change it
    # files a correction like everybody else (D3, D6).
    if row["owner"] != "fleetops_migrator" or row["actor"] != "fleetops_migrator":
        raise RuntimeError("History grants must be applied by the fleetops_migrator table owner")
    quote = connection.dialect.identifier_preparer.quote_identifier
    qualified = f"{quote(schema)}.{quote(table)}"
    columns = (
        connection.execute(
            text(
                "SELECT a.attname FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relname = :table "
                "AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum"
            ),
            {"schema": schema, "table": table},
        )
        .scalars()
        .all()
    )
    # Table-level REVOKE does not touch column-level ACLs. A stray GRANT UPDATE (col)
    # from an earlier migration would survive and silently reopen the history table, so
    # every live column is revoked explicitly before the SELECT/INSERT pattern is applied.
    connection.exec_driver_sql(
        f"REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM PUBLIC, fleetops_app"
    )
    if columns:
        column_list = ", ".join(quote(column) for column in columns)
        connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES ({column_list}) ON TABLE {qualified} FROM PUBLIC, fleetops_app"
        )
    connection.exec_driver_sql(f"GRANT SELECT, INSERT ON TABLE {qualified} TO fleetops_app")
