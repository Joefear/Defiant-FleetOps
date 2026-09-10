"""Actual runtime roles prove ADR-008 privileges, tenancy, constraints and credential binding."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice6.conftest import for_asset
from server.tests.slice7.conftest import (
    TABLES,
    call_assignment,
    config_values,
    event_values,
    runtime_assignment,
    runtime_configuration,
    witness_values,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.auth import token_digest
from fleetops.db.metadata import asset_assignment_events as events
from fleetops.db.metadata import asset_configurations as configs
from fleetops.db.metadata import asset_initial_assignment_facts as witnesses
from fleetops.db.metadata import assets
from fleetops.db.tenancy import apply_tenant_policy, set_credential_context, set_organization


def values(table, tenant, **changes):
    """Administrative fixture inputs do not stand in for the runtime role under test."""
    if table is witnesses:
        return witness_values(tenant, **changes)
    if table is events:
        return event_values(tenant, **changes)
    return config_values(tenant, applied_by=tenant.actor_id, **changes)


@pytest.fixture
def isolated_dml(migrator_connection, app_connection):
    """Separate actual RLS behavior from narrower production ACL denials, then restore both."""
    for table in TABLES:
        migrator_connection.exec_driver_sql(
            f"GRANT INSERT, UPDATE, DELETE ON fleetops.{table.name} TO fleetops_app"
        )
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        for table in TABLES:
            apply_tenant_policy(migrator_connection, table, privileges=("SELECT",))
        migrator_connection.exec_driver_sql("""
            GRANT INSERT (id, org_id, asset_id, image_name, image_version, config_profile,
                          notes, applied_at, evidence_ref)
            ON fleetops.asset_configurations TO fleetops_app
        """)
        migrator_connection.commit()


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE", "INSERT"])
@pytest.mark.parametrize("role", ["fleetops_app", "fleetops_authenticator"])
def test_no_direct_history_mutation_or_witness_creation(
    table,
    operation,
    role,
    asset_data,
    app_connection,
    authenticator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    connection = app_connection if role == "fleetops_app" else authenticator_connection
    # Configuration ordinary INSERT is intentionally permitted and tested independently.
    if table is configs and operation == "INSERT" and role == "fleetops_app":
        row = runtime_configuration(connection, a)
        assert row["applied_by"] == a.actor_id
        return
    with pytest.raises(DBAPIError) as error, connection.begin():
        if role == "fleetops_app":
            set_authenticated(connection, a)
        else:
            set_organization(connection, a.org_id)
        if operation == "INSERT":
            connection.execute(table.insert().values(values(table, a)))
        elif operation == "UPDATE":
            connection.execute(table.update().values(recorded_at=text("statement_timestamp()")))
        elif operation == "DELETE":
            connection.execute(table.delete())
        else:
            connection.exec_driver_sql(f"TRUNCATE fleetops.{table.name}")
    assert error.value.orig.sqlstate == "42501"
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("column", ["current_assignment_id", "version"])
def test_runtime_projection_updates_are_denied(
    column,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error, app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.execute(
            assets.update()
            .where(assets.c.id == a.asset_id)
            .values(**{column: None if column == "current_assignment_id" else 2})
        )
    assert error.value.orig.sqlstate == "42501"
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("scope", ["same", "cross", "none"])
def test_rls_reads_and_invisible_updates_deletes(
    table,
    scope,
    asset_data,
    app_connection,
    migrator_connection,
    isolated_dml,
):
    a, b = asset_data
    if table is not witnesses:
        for tenant in (a, b):
            migrator_connection.execute(table.insert().values(values(table, tenant)))
        migrator_connection.commit()
    before = [dict(r) for r in migrator_connection.execute(select(table)).mappings()]
    migrator_connection.rollback()
    with app_connection.begin():
        if scope != "none":
            set_authenticated(app_connection, a)
        target = a if scope == "same" else b
        visible = app_connection.execute(
            select(table).where(table.c.asset_id == target.asset_id)
        ).all()
        assert len(visible) == (1 if scope == "same" else 0)
        if scope != "same":
            assert (
                app_connection.execute(
                    table.update()
                    .where(table.c.asset_id == target.asset_id)
                    .values(recorded_at=text("statement_timestamp()"))
                ).rowcount
                == 0
            )
            assert (
                app_connection.execute(
                    table.delete().where(table.c.asset_id == target.asset_id)
                ).rowcount
                == 0
            )
    assert [dict(r) for r in migrator_connection.execute(select(table)).mappings()] == before


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("scope", ["cross", "none"])
def test_with_check_rejects_insert_before_attribution_or_fk_checks(
    table,
    scope,
    asset_data,
    seed_asset,
    app_connection,
    isolated_dml,
):
    a, b = asset_data
    target = for_asset(b, seed_asset(b, initial_assignment=False)["id"])
    with pytest.raises(DBAPIError) as error, app_connection.begin():
        if scope == "cross":
            set_authenticated(app_connection, a)
        app_connection.execute(table.insert().values(values(table, target)))
    assert error.value.orig.sqlstate == "42501"
    assert "row-level security policy" in error.value.orig.diag.message_primary


@pytest.mark.parametrize("table", TABLES, ids=lambda t: t.name)
@pytest.mark.parametrize("reference", ["asset", "actor"])
def test_composite_tenant_references_reject_cross_org(
    table,
    reference,
    asset_data,
    seed_asset,
    migrator_connection,
):
    a, b = asset_data
    target = for_asset(a, seed_asset(a, initial_assignment=False)["id"])
    row = values(table, target)
    field = "asset_id" if reference == "asset" else "applied_by" if table is configs else "actor_id"
    # Use an unwitnessed foreign Asset so witness uniqueness cannot mask the tenant FK.
    row[field] = (
        seed_asset(b, initial_assignment=False)["id"] if reference == "asset" else b.actor_id
    )
    with pytest.raises(DBAPIError) as error, migrator_connection.begin():
        migrator_connection.execute(table.insert().values(row))
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("kind", ["ACTOR", "LOCATION", "PARTY"])
@pytest.mark.parametrize("invalid", ["cross-org", "wrong-type", "missing"])
def test_assignment_polymorphic_target_is_durably_validated(
    kind,
    invalid,
    asset_data,
    app_connection,
    assignment_snapshot,
):
    a, b = asset_data
    target = {"ACTOR": b.actor_id, "LOCATION": b.location_id, "PARTY": b.party_id}[kind]
    if invalid == "wrong-type":
        target = a.actor_id if kind != "ACTOR" else a.party_id
    elif invalid == "missing":
        target = uuid7()
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_assignment(app_connection, a, assignee_type=kind, assignee_id=target)
    assert error.value.orig.sqlstate == "23503"
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("endpoint", ["from", "to"])
@pytest.mark.parametrize("invalid", ["type-only", "id-only", "unsupported"])
def test_assignment_pair_checks_apply_even_to_owner_insert(
    endpoint,
    invalid,
    asset_data,
    migrator_connection,
):
    a = asset_data[0]
    changes = {f"{endpoint}_assignee_type": "ACTOR", f"{endpoint}_assignee_id": a.actor_id}
    changes[
        f"{endpoint}_assignee_id" if invalid == "type-only" else f"{endpoint}_assignee_type"
    ] = "USER" if invalid == "unsupported" else None
    with pytest.raises(DBAPIError) as error, migrator_connection.begin():
        migrator_connection.execute(events.insert().values(event_values(a, **changes)))
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize("value,code", [(None, "23502"), (0, "23514"), (-1, "23514"), (2, "23505")])
def test_event_result_version_is_required_positive_and_unique(
    value,
    code,
    asset_data,
    migrator_connection,
):
    a = asset_data[0]
    migrator_connection.execute(events.insert().values(event_values(a)))
    migrator_connection.commit()
    with pytest.raises(DBAPIError) as error, migrator_connection.begin():
        migrator_connection.execute(events.insert().values(event_values(a, result_version=value)))
    assert error.value.orig.sqlstate == code


@pytest.mark.parametrize("scope", ["asset", "tenant"])
def test_correction_reference_cannot_cross_asset_or_tenant(
    scope,
    asset_data,
    seed_asset,
    migrator_connection,
):
    a, b = asset_data
    original = event_values(a)
    migrator_connection.execute(events.insert().values(original))
    migrator_connection.commit()
    target = b if scope == "tenant" else for_asset(a, seed_asset(a)["id"])
    with pytest.raises(DBAPIError) as error, migrator_connection.begin():
        migrator_connection.execute(
            events.insert().values(
                event_values(target, corrects_assignment_event_id=original["id"])
            )
        )
    assert error.value.orig.sqlstate == "23503"


@pytest.mark.parametrize("unassign", [False, True])
@pytest.mark.parametrize(
    "authority", ["valid", "actor-guc", "org-only", "cross-asset", "sql-actor"]
)
def test_assignment_credential_boundary_cannot_choose_another_performer(
    unassign,
    authority,
    asset_data,
    other_human,
    app_connection,
    assignment_snapshot,
):
    a, b = asset_data
    if unassign:
        runtime_assignment(app_connection, a)
    before = assignment_snapshot(a)

    def perform():
        with app_connection.begin():
            if authority == "org-only":
                set_organization(app_connection, a.org_id)
            else:
                set_authenticated(app_connection, a)
            if authority == "actor-guc":
                app_connection.execute(
                    text("SELECT set_config('fleetops.actor_id', :id, true)"),
                    {"id": str(other_human.actor_id)},
                )
            if authority == "sql-actor":
                # A caller-selected Actor overload must not exist.
                name = "unassign_asset" if unassign else "assign_asset"
                return app_connection.execute(
                    text(f"SELECT fleetops.{name}(actor_id => :actor)"),
                    {"actor": other_human.actor_id},
                )
            return call_assignment(
                app_connection,
                a,
                unassign=unassign,
                expected_version=2 if unassign else 1,
                asset_id=b.asset_id if authority == "cross-asset" else a.asset_id,
            )

    if authority in {"valid", "actor-guc"}:
        assert perform()["actor_id"] == a.actor_id
    else:
        with pytest.raises(DBAPIError) as error:
            perform()
        assert (
            error.value.orig.sqlstate
            == {"org-only": "42501", "cross-asset": "P0002", "sql-actor": "42883"}[authority]
        )
        assert assignment_snapshot(a) == before


@pytest.mark.parametrize("table", [witnesses, configs], ids=lambda t: t.name)
@pytest.mark.parametrize("authority", ["valid", "spoof", "org-only"])
def test_ordinary_history_actor_is_database_bound_under_isolated_grants(
    table,
    authority,
    asset_data,
    seed_asset,
    other_human,
    app_connection,
    migrator_connection,
    isolated_dml,
):
    a = for_asset(asset_data[0], seed_asset(asset_data[0], initial_assignment=False)["id"])
    row = values(table, a)
    actor_column = "applied_by" if table is configs else "actor_id"
    row[actor_column] = other_human.actor_id if authority == "spoof" else a.actor_id

    def insert():
        with app_connection.begin():
            if authority == "org-only":
                set_organization(app_connection, a.org_id)
            else:
                set_authenticated(app_connection, a)
            app_connection.execute(table.insert().values(row))

    if authority == "valid":
        insert()
        assert (
            migrator_connection.execute(
                select(table.c[actor_column]).where(table.c.asset_id == a.asset_id)
            ).scalar_one()
            == a.actor_id
        )
    else:
        with pytest.raises(DBAPIError) as error:
            insert()
        assert error.value.orig.sqlstate == "42501"
        assert (
            migrator_connection.execute(select(table).where(table.c.asset_id == a.asset_id)).all()
            == []
        )


@pytest.mark.parametrize("unassign", [False, True])
def test_authenticator_cannot_execute_assignment(
    unassign,
    asset_data,
    authenticator_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error, authenticator_connection.begin():
        set_organization(authenticator_connection, a.org_id)
        call_assignment(authenticator_connection, a, unassign=unassign)
    assert error.value.orig.sqlstate == "42501"
    assert assignment_snapshot(a) == before


@pytest.mark.parametrize("operation", ["assign", "unassign", "configuration"])
@pytest.mark.parametrize("authority", ["missing", "org-only", "invalid", "actor-guc"])
def test_raw_runtime_operations_revalidate_credential_and_ignore_actor_guc(
    operation,
    authority,
    asset_data,
    other_human,
    app_connection,
    assignment_snapshot,
):
    a = asset_data[0]
    if operation == "unassign":
        runtime_assignment(app_connection, a)
    before = assignment_snapshot(a)

    def perform():
        with app_connection.begin():
            if authority != "missing":
                set_organization(app_connection, a.org_id)
            if authority == "invalid":
                set_credential_context(app_connection, token_digest("unissued"))
            if authority == "actor-guc":
                set_authenticated(app_connection, a)
                app_connection.execute(
                    text("SELECT set_config('fleetops.actor_id', :actor, true)"),
                    dict(actor=str(other_human.actor_id)),
                )
            if operation == "configuration":
                return (
                    app_connection.execute(
                        configs.insert().values(config_values(a)).returning(configs)
                    )
                    .mappings()
                    .one()
                )
            return call_assignment(
                app_connection,
                a,
                unassign=operation == "unassign",
                expected_version=before["asset"]["version"],
            )

    if authority == "actor-guc":
        field = "applied_by" if operation == "configuration" else "actor_id"
        assert perform()[field] == a.actor_id
    else:
        with pytest.raises(DBAPIError) as error:
            perform()
        assert error.value.orig.sqlstate == "42501"
        assert assignment_snapshot(a) == before


@pytest.mark.parametrize("kind", ["ACTOR", "LOCATION", "PARTY"])
@pytest.mark.parametrize("unassign", [False, True])
def test_prior_polymorphic_target_is_validated_before_appending_again(
    kind,
    unassign,
    asset_data,
    app_connection,
    migrator_connection,
    assignment_snapshot,
):
    a, b = asset_data
    first = runtime_assignment(app_connection, a)
    # Bypass admission only as the administrative corruption fixture. The projection
    # still points at the latest event, so checking that ID alone cannot pass this test.
    target = {"ACTOR": b.actor_id, "LOCATION": b.location_id, "PARTY": b.party_id}[kind]
    migrator_connection.execute(
        events.update()
        .where(events.c.id == first["id"])
        .values(
            to_assignee_type=kind,
            to_assignee_id=target,
        )
    )
    migrator_connection.commit()
    before = assignment_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_assignment(app_connection, a, expected_version=2, unassign=unassign)
    assert error.value.orig.sqlstate == "23503"
    assert assignment_snapshot(a) == before
