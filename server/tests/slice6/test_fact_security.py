"""Real runtime roles prove append-only facts, credential binding and tenant-safe references."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice6.conftest import (
    KINDS,
    MOVE,
    OWNERSHIP,
    TABLES,
    baseline_values,
    call_fact,
    for_asset,
    history_values,
    runtime_fact,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.auth import token_digest
from fleetops.db.metadata import actors, asset_initial_facts, sessions, users
from fleetops.db.tenancy import set_credential_context, set_organization
from fleetops.domain.asset_facts import reconcile_assets


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("operation", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
@pytest.mark.parametrize("role", ["fleetops_app", "fleetops_authenticator"])
def test_no_direct_runtime_fact_mutation(
    table,
    operation,
    role,
    asset_data,
    app_connection,
    authenticator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    connection = app_connection if role == "fleetops_app" else authenticator_connection
    before = fact_snapshot(a)
    statement = {
        "INSERT": f"INSERT INTO fleetops.{table.name} DEFAULT VALUES",
        "UPDATE": f"UPDATE fleetops.{table.name} SET org_id=org_id",
        "DELETE": f"DELETE FROM fleetops.{table.name}",
        "TRUNCATE": f"TRUNCATE fleetops.{table.name}",
    }[operation]
    with pytest.raises(DBAPIError) as error:
        with connection.begin():
            if role == "fleetops_app":
                set_authenticated(connection, a)
            else:
                set_organization(connection, a.org_id)
            connection.exec_driver_sql(statement)
    assert error.value.orig.sqlstate == "42501"
    assert fact_snapshot(a) == before


@pytest.mark.parametrize(
    "column", ["current_location_id", "custodian_party_id", "owner_party_id", "version"]
)
def test_physical_projection_writes_remain_denied(
    column,
    asset_data,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.exec_driver_sql(f"UPDATE fleetops.assets SET {column}={column}")
    assert error.value.orig.sqlstate == "42501"
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_atomic_function_catalog_and_authenticator_boundary(
    kind,
    asset_data,
    migrator_connection,
    authenticator_connection,
):
    rows = (
        migrator_connection.execute(
            text("""
        SELECT p.prosecdef, p.provolatile, p.proconfig, pg_get_userbyid(p.proowner) AS owner,
               p.proargnames, p.pronargs, rn.nspname || '.' || rt.typname AS result
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_type rt ON rt.oid=p.prorettype JOIN pg_namespace rn ON rn.oid=rt.typnamespace
        WHERE n.nspname='fleetops' AND p.proname=:name
    """),
            {"name": kind.function},
        )
        .mappings()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["prosecdef"] and row["provolatile"] == "v"
    assert row["owner"] == "fleetops_migrator"
    assert row["proconfig"] == ["search_path=pg_catalog, pg_temp"]
    assert row["pronargs"] == 7
    assert row["proargnames"] == [
        "p_asset_id",
        "p_expected_version",
        f"p_{kind.to_field}",
        "p_reason",
        "p_occurred_at",
        "p_client_op_id",
        "p_history_id",
    ]
    assert row["result"] == f"fleetops.{kind.table.name}"
    grants = migrator_connection.execute(
        text("""
        SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(acl.grantee) END,
               acl.privilege_type
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(p.proacl) acl
        WHERE n.nspname='fleetops' AND p.proname=:name
    """),
        {"name": kind.function},
    ).all()
    assert set(grants) == {("fleetops_migrator", "EXECUTE"), ("fleetops_app", "EXECUTE")}
    migrator_connection.rollback()
    with pytest.raises(DBAPIError) as error:
        with authenticator_connection.begin():
            set_organization(authenticator_connection, asset_data[0].org_id)
            call_fact(authenticator_connection, asset_data[0], kind)
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("target", ["cross-tenant", "missing"])
def test_target_reference_and_invisible_asset_fail_closed(
    kind,
    target,
    asset_data,
    app_connection,
    fact_snapshot,
):
    a, b = asset_data
    before = fact_snapshot(a), fact_snapshot(b)
    fact_id = (
        (b.location_id if kind is MOVE else b.party_id) if target == "cross-tenant" else uuid7()
    )
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, a, kind, target=fact_id)
    assert error.value.orig.sqlstate == "23503"
    messages = []
    for asset_id in (b.asset_id, uuid7()):
        with pytest.raises(DBAPIError) as error:
            runtime_fact(app_connection, a, kind, asset_id=asset_id)
        assert error.value.orig.sqlstate == "P0002"
        messages.append(error.value.orig.diag.message_primary)
    assert messages == ["Asset not found", "Asset not found"]
    assert (fact_snapshot(a), fact_snapshot(b)) == before


def test_owner_cannot_be_cleared(asset_data, app_connection, fact_snapshot):
    a = asset_data[0]
    before = fact_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, a, OWNERSHIP, target=None)
    assert error.value.orig.sqlstate == "23502"
    assert fact_snapshot(a) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "no-context",
        "org-only",
        "unknown",
        "malformed",
        "cross-org",
        "expired",
        "revoked",
        "inactive-user",
        "inactive-actor",
    ],
)
@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_no_invalid_credential_can_attribute_physical_history(
    invalid,
    kind,
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a, b = asset_data
    if invalid in {"expired", "revoked"}:
        values = {
            "created_at": text("statement_timestamp() - interval '2 minutes'"),
            "expires_at": text("statement_timestamp() - interval '1 minute'"),
        }
        if invalid == "revoked":
            values = {"active": False}
        migrator_connection.execute(
            sessions.update().where(sessions.c.org_id == a.org_id).values(**values)
        )
    elif invalid == "inactive-user":
        migrator_connection.execute(
            users.update().where(users.c.id == a.user_id).values(active=False)
        )
    elif invalid == "inactive-actor":
        migrator_connection.execute(
            actors.update().where(actors.c.id == a.actor_id).values(active=False)
        )
    migrator_connection.commit()
    before = fact_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if invalid == "org-only":
                set_organization(app_connection, a.org_id)
            elif invalid != "no-context":
                set_authenticated(app_connection, a)
                if invalid == "unknown":
                    set_credential_context(app_connection, token_digest("unissued"))
                elif invalid == "cross-org":
                    set_credential_context(app_connection, token_digest(b.raw_token))
                elif invalid == "malformed":
                    app_connection.exec_driver_sql(
                        "SELECT set_config('fleetops.session_digest_hex', 'not-a-digest', true)"
                    )
            call_fact(app_connection, a, kind)
    assert error.value.orig.sqlstate == "42501"
    assert fact_snapshot(a) == before


def test_actor_guc_and_sql_parameter_cannot_select_another_same_org_actor(
    asset_data,
    other_human,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    for version, kind in enumerate(KINDS, 1):
        before = fact_snapshot(a)
        # A named Actor argument has no executable overload, even for another valid HUMAN.
        with pytest.raises(DBAPIError) as error:
            with app_connection.begin():
                set_authenticated(app_connection, a)
                app_connection.execute(
                    text(f"""
                    SELECT fleetops.{kind.function}(
                        p_asset_id => :asset, p_expected_version => :version,
                        p_{kind.to_field} => :target, p_reason => 'spoof',
                        p_occurred_at => now(), p_client_op_id => NULL,
                        p_history_id => :history, p_actor_id => :actor)
                """),
                    dict(
                        asset=a.asset_id,
                        version=version,
                        target=getattr(a, kind.target_attribute),
                        history=uuid7(),
                        actor=other_human.actor_id,
                    ),
                )
        assert error.value.orig.sqlstate == "42883"
        assert fact_snapshot(a) == before
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                text("SELECT set_config('fleetops.actor_id', :actor, true)"),
                {"actor": str(other_human.actor_id)},
            )
            row = call_fact(app_connection, a, kind, expected_version=version)
        assert row["actor_id"] == a.actor_id


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("context", ["other-org", "none"])
def test_rls_visibility_and_hidden_mutation_are_fail_closed(
    table,
    context,
    asset_data,
    fact_dml,
    app_connection,
    migrator_connection,
):
    a, b = asset_data
    if table is not asset_initial_facts:
        kind = next(k for k in KINDS if k.table is table)
        migrator_connection.execute(table.insert().values(history_values(b, kind)))
        migrator_connection.commit()
    before = migrator_connection.execute(select(table).where(table.c.org_id == b.org_id)).all()
    migrator_connection.rollback()
    with app_connection.begin():
        if context == "other-org":
            set_authenticated(app_connection, a)
        assert app_connection.execute(select(table).where(table.c.org_id == b.org_id)).all() == []
        assert (
            app_connection.execute(
                table.update().where(table.c.org_id == b.org_id).values(org_id=b.org_id)
            ).rowcount
            == 0
        )
        assert (
            app_connection.execute(table.delete().where(table.c.org_id == b.org_id)).rowcount == 0
        )
        if context == "none":
            assert app_connection.execute(select(table)).all() == []
            assert reconcile_assets(app_connection) == []
    assert (
        migrator_connection.execute(select(table).where(table.c.org_id == b.org_id)).all() == before
    )


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("context", ["wrong-org", "none"])
def test_rls_with_check_rejects_inserts_even_with_test_only_grants(
    table,
    context,
    asset_data,
    seed_asset,
    fact_dml,
    app_connection,
):
    a, b = asset_data
    target = for_asset(b, seed_asset(b, initial_facts=False)["id"])
    values = (
        baseline_values(target)
        if table is asset_initial_facts
        else history_values(target, next(k for k in KINDS if k.table is table))
    )
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if context == "wrong-org":
                set_authenticated(app_connection, a)
            app_connection.execute(table.insert().values(values))
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
def test_rls_with_check_rejects_moving_a_visible_row_to_other_tenant(
    table,
    asset_data,
    fact_dml,
    app_connection,
    migrator_connection,
):
    a, b = asset_data
    if table is not asset_initial_facts:
        migrator_connection.execute(
            table.insert().values(history_values(a, next(k for k in KINDS if k.table is table)))
        )
        migrator_connection.commit()
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                table.update().where(table.c.org_id == a.org_id).values(org_id=b.org_id)
            )
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security" in error.value.orig.diag.message_primary


REFERENCE_CASES = [
    (asset_initial_facts, field, target)
    for field, target in (
        ("asset_id", "asset_id"),
        ("initial_owner_party_id", "party_id"),
        ("initial_custodian_party_id", "party_id"),
        ("initial_location_id", "location_id"),
        ("actor_id", "actor_id"),
    )
] + [
    (kind.table, field, target)
    for kind in KINDS
    for field, target in (
        ("asset_id", "asset_id"),
        ("actor_id", "actor_id"),
        (kind.from_field, "location_id" if kind is MOVE else "party_id"),
        (kind.to_field, "location_id" if kind is MOVE else "party_id"),
    )
]


@pytest.mark.parametrize(
    "table,field,target", REFERENCE_CASES, ids=[f"{t.name}-{f}" for t, f, _ in REFERENCE_CASES]
)
def test_composite_references_reject_cross_tenant_uuid(
    table,
    field,
    target,
    asset_data,
    seed_asset,
    migrator_connection,
):
    a, b = asset_data
    tenant = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    values = (
        baseline_values(tenant)
        if table is asset_initial_facts
        else history_values(tenant, next(k for k in KINDS if k.table is table))
    )
    values[field] = getattr(b, target)
    # Remove only B's baseline to ensure a foreign-Asset probe reaches the FK, not PK uniqueness.
    if table is asset_initial_facts and field == "asset_id":
        migrator_connection.execute(table.delete().where(table.c.asset_id == b.asset_id))
        migrator_connection.commit()
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(table.insert().values(values))
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("other", ["asset", "tenant"])
def test_correction_seam_cannot_reference_other_asset_or_tenant(
    kind,
    other,
    asset_data,
    seed_asset,
    migrator_connection,
):
    a, b = asset_data
    original = history_values(b if other == "tenant" else a, kind)
    migrator_connection.execute(kind.table.insert().values(original))
    migrator_connection.commit()
    target = for_asset(a, seed_asset(a)["id"])
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                kind.table.insert().values(
                    history_values(target, kind, **{kind.correction: original["id"]})
                )
            )
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("authority", ["valid", "spoof", "org-only"])
def test_baseline_actor_trigger_is_durable_under_isolated_insert_grants(
    authority,
    asset_data,
    seed_asset,
    other_human,
    fact_dml,
    app_connection,
    migrator_connection,
):
    a = asset_data[0]
    tenant = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    values = baseline_values(
        tenant, actor_id=other_human.actor_id if authority == "spoof" else a.actor_id
    )

    def insert():
        with app_connection.begin():
            if authority == "org-only":
                set_organization(app_connection, a.org_id)
            else:
                set_authenticated(app_connection, a)
            app_connection.execute(asset_initial_facts.insert().values(values))

    if authority == "valid":
        insert()
        assert (
            migrator_connection.execute(
                select(asset_initial_facts.c.actor_id).where(
                    asset_initial_facts.c.asset_id == tenant.asset_id
                )
            ).scalar_one()
            == a.actor_id
        )
    else:
        with pytest.raises(DBAPIError) as error:
            insert()
        assert error.value.orig.sqlstate == "42501"
        assert (
            migrator_connection.execute(
                select(asset_initial_facts).where(asset_initial_facts.c.asset_id == tenant.asset_id)
            ).all()
            == []
        )


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("value,code", [(None, "23502"), (0, "23514"), (-1, "23514"), (2, "23505")])
def test_history_result_version_is_required_positive_and_unique(
    kind,
    value,
    code,
    asset_data,
    migrator_connection,
):
    a = asset_data[0]
    migrator_connection.execute(kind.table.insert().values(history_values(a, kind)))
    migrator_connection.commit()
    with pytest.raises(DBAPIError) as error:
        with migrator_connection.begin():
            migrator_connection.execute(
                kind.table.insert().values(history_values(a, kind, result_version=value))
            )
    assert error.value.orig.sqlstate == code
