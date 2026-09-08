"""Reusable D18 row security and explicit runtime grants, applied in migration transactions."""

from uuid import UUID

from sqlalchemy import Connection, Table, text

RUNTIME_GRANTS = {
    "organizations": ("SELECT",),
    "actors": ("SELECT", "INSERT"),
    "parties": ("SELECT", "INSERT"),
    "party_roles": ("SELECT", "INSERT"),
    "users": (),
    "sessions": (),
}


def apply_tenant_policy(
    connection: Connection, table: Table, *, privileges: tuple[str, ...] = ("SELECT", "INSERT")
) -> None:
    """Install fail-closed USING/WITH CHECK and replace stale table AND column ACLs.

    RLS does not protect TRUNCATE. Restrict this helper to ordinary DML and remove all
    pre-existing app/PUBLIC grants before granting exactly what this slice needs.
    A restrictive boundary also prevents a later permissive policy from OR-ing tenancy
    away. Owners remain privileged for migrations and the ADR-approved function boundaries.
    """
    if not set(privileges) <= {"SELECT", "INSERT", "UPDATE", "DELETE"}:
        raise ValueError("Only ordinary DML privileges are permitted")
    owner = connection.execute(
        text(
            "SELECT pg_get_userbyid(c.relowner), current_user FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema AND c.relname = :table AND c.relkind = 'r'"
        ),
        {"schema": table.schema, "table": table.name},
    ).one()
    if tuple(owner) != ("fleetops_migrator", "fleetops_migrator"):
        raise RuntimeError("Tenant policy/grants require the fleetops_migrator table owner")
    quote = connection.dialect.identifier_preparer.quote_identifier
    qualified = f"{quote(table.schema)}.{quote(table.name)}"
    # Reflect live columns: migration metadata may omit a column left by older DDL,
    # and a column ACL survives a table-level REVOKE independently.
    live_columns = connection.execute(
        text(
            "SELECT attname FROM pg_attribute WHERE attrelid = CAST(:table AS regclass) "
            "AND attnum > 0 AND NOT attisdropped ORDER BY attnum"
        ),
        {"table": qualified},
    ).scalars()
    columns = ", ".join(quote(name) for name in live_columns)
    connection.exec_driver_sql(f"REVOKE ALL ON TABLE {qualified} FROM PUBLIC, fleetops_app")
    connection.exec_driver_sql(
        f"REVOKE ALL PRIVILEGES ({columns}) ON TABLE {qualified} FROM PUBLIC, fleetops_app"
    )
    connection.exec_driver_sql(f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY")
    for name in ("tenant_access", "tenant_boundary"):
        connection.exec_driver_sql(f"DROP POLICY IF EXISTS {name} ON {qualified}")
    # SET LOCAL resets an already-used custom setting to an empty string at transaction
    # end. NULLIF handles both never-set and previously-set connections without a fallback.
    predicate = "org_id = NULLIF(pg_catalog.current_setting('fleetops.org_id', true), '')::uuid"
    connection.exec_driver_sql(
        f"CREATE POLICY tenant_access ON {qualified} TO fleetops_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    connection.exec_driver_sql(
        f"CREATE POLICY tenant_boundary ON {qualified} AS RESTRICTIVE TO fleetops_app "
        f"USING ({predicate}) WITH CHECK ({predicate})"
    )
    if privileges:
        connection.exec_driver_sql(
            f"GRANT {', '.join(privileges)} ON TABLE {qualified} TO fleetops_app"
        )


def set_organization(connection: Connection, organization_id: UUID) -> None:
    """Represent already-trusted identity inside the transaction doing tenant work.

    This is not authentication. Only authenticated context or the explicitly trusted
    login/bootstrap configuration may reach this helper; request fields are not authority.
    """
    if not connection.in_transaction():
        raise RuntimeError("Organization context requires an active transaction")
    connection.execute(
        text("SELECT pg_catalog.set_config('fleetops.org_id', :org_id, true)"),
        {"org_id": str(organization_id)},
    )


def set_credential_context(connection: Connection, digest: bytes) -> None:
    """Carry a credential, never an Actor assertion, for database re-resolution (ADR-006).

    Parameter binding keeps the digest out of SQL statement text; runtime engines hide
    parameters. SET LOCAL ends with the request transaction, including pool rollback.
    """
    if not isinstance(digest, bytes) or len(digest) != 32:
        raise ValueError("Credential context requires one SHA-256 digest")
    if not connection.in_transaction():
        raise RuntimeError("Credential context requires an active transaction")
    connection.execute(
        text("SELECT pg_catalog.set_config('fleetops.session_digest_hex', :digest, true)"),
        {"digest": digest.hex()},
    )
