"""Durable tenant references, migration seeds, and privilege boundaries."""

import pytest
from conftest import TABLES
from sqlalchemy import Column, MetaData, Table, Uuid, select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.auth import hash_password, token_digest
from fleetops.db.metadata import actors, organizations, parties, party_roles, sessions, users
from fleetops.db.seed import ACTOR_ID, ORGANIZATION_ID, PARTY_ID
from fleetops.db.tenancy import RUNTIME_GRANTS, apply_tenant_policy, set_organization
from fleetops.domain.actor_types import ActorType
from fleetops.domain.identity import create_actor, create_party
from fleetops.domain.party_roles import PartyRole


def test_slice2_migration_round_trip_and_seed_contract(database, migrator_connection):
    try:
        result = database.migrate("downgrade", "0001_empty_baseline")
        assert result.returncode == 0, result.stderr
        assert migrator_connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
        ).all() == [("alembic_version",)]
        migrator_connection.rollback()
        result = database.migrate("upgrade", "0002_identity_auth")
        assert result.returncode == 0, result.stderr
        assert migrator_connection.execute(select(organizations.c.id)).all() == [(ORGANIZATION_ID,)]
        assert migrator_connection.execute(select(actors.c.id, actors.c.type)).all() == [
            (ACTOR_ID, "HUMAN"),
        ]
        assert migrator_connection.execute(select(parties.c.id)).all() == [(PARTY_ID,)]
        assert migrator_connection.execute(select(party_roles.c.role)).all() == [("INTERNAL",)]
        assert migrator_connection.execute(select(users)).all() == []
        assert migrator_connection.execute(select(sessions)).all() == []
        migrator_connection.rollback()
        # Metadata check targets current head; the seed assertions above remain at 0002.
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr
        result = database.migrate("check")
        assert result.returncode == 0, result.stderr
    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr


def test_only_slice2_tables_and_approved_resolver_exist(database, migrator_connection):
    # Inspect the actual historical revision, not an expanded definition of Slice 2.
    try:
        result = database.migrate("downgrade", "0002_identity_auth")
        assert result.returncode == 0, result.stderr
        expected = {"organizations", "actors", "parties", "party_roles", "users", "sessions"}
        assert set(TABLES) == expected
        assert set(
            migrator_connection.exec_driver_sql(
                "SELECT tablename FROM pg_tables WHERE schemaname='fleetops'"
            ).scalars()
        ) == expected | {"alembic_version"}
        assert migrator_connection.exec_driver_sql(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='fleetops'"
        ).all() == [("resolve_session",)]

    finally:
        migrator_connection.rollback()
        result = database.migrate("upgrade", "head")
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", sorted(TABLES))
def test_tenant_tables_are_owned_scoped_and_default_deny(name, migrator_connection, app_connection):
    assert migrator_connection.exec_driver_sql(
        "SELECT pg_get_userbyid(relowner), relrowsecurity FROM pg_class WHERE oid=%s::regclass",
        (f"fleetops.{name}",),
    ).one() == ("fleetops_migrator", True)
    policies = migrator_connection.exec_driver_sql(
        "SELECT permissive, qual, with_check FROM pg_policies "
        "WHERE schemaname='fleetops' AND tablename=%s",
        (name,),
    ).all()
    assert {row.permissive for row in policies} == {"PERMISSIVE", "RESTRICTIVE"}
    for row in policies:
        assert "fleetops.org_id" in row.qual
        assert row.qual == row.with_check
    for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
        assert app_connection.exec_driver_sql(
            "SELECT has_table_privilege(current_user, %s, %s)",
            (f"fleetops.{name}", privilege),
        ).scalar_one() is (privilege in RUNTIME_GRANTS[name])
    if name == "sessions":
        for column in TABLES[name].columns:
            assert app_connection.exec_driver_sql(
                "SELECT has_column_privilege(current_user, %s, %s, 'UPDATE')",
                ("fleetops.sessions", column.name),
            ).scalar_one() is (column.name == "active")
    assert (
        migrator_connection.exec_driver_sql(
            "SELECT count(*) FROM pg_class c, "
            "LATERAL aclexplode(COALESCE(c.relacl, acldefault('r', c.relowner))) acl "
            "WHERE c.oid=%s::regclass AND acl.grantee=0",
            (f"fleetops.{name}",),
        ).scalar_one()
        == 0
    )


@pytest.mark.parametrize("name", ["actors", "parties", "party_roles", "users", "sessions"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_cross_tenant_foreign_keys_reject_otherwise_valid_ids(
    name,
    operation,
    tenants,
    permitted_dml,
    app_connection,
):
    a, b = tenants
    table = TABLES[name]
    values = {
        "actors": dict(type="HUMAN", display_name="probe", created_by_actor_id=b.ids["actors"]),
        "parties": dict(display_name="probe", created_by_actor_id=b.ids["actors"]),
        "party_roles": dict(party_id=b.ids["parties"], role="VENDOR"),
        "users": dict(
            actor_id=b.ids["actors"],
            username="probe",
            password_hash=hash_password(a.password),
        ),
        "sessions": dict(
            user_id=b.ids["users"],
            token_digest=token_digest("unissued-constraint-probe"),
            expires_at="2099-01-01T00:00:00+00:00",
        ),
    }
    # The proposed row belongs to A and passes RLS. Only its reference crosses tenants,
    # so a 23503 here proves a composite FK rather than an application tenant filter.
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            statement = (
                table.insert().values(id=uuid7(), org_id=a.org_id, **values[name])
                if operation == "insert"
                else table.update().where(table.c.id == a.ids[name]).values(**values[name])
            )
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "23503"


def test_device_actor_has_no_user_and_can_be_attributed(tenants, app_connection):
    a, _ = tenants
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        device = create_actor(
            app_connection,
            org_id=a.org_id,
            performer_id=a.ids["actors"],
            actor_type=ActorType.DEVICE,
            display_name="Test device",
        )
        assert (
            app_connection.execute(select(users.c.id).where(users.c.actor_id == device["id"])).all()
            == []
        )
        # This trusted in-process test attributes a persistence write to a typed actor;
        # it does not invent a device login or an external-integration authentication path.
        party = create_party(
            app_connection,
            org_id=a.org_id,
            performer_id=device["id"],
            display_name="Attributed record",
            roles={PartyRole.INTERNAL},
        )
        assert app_connection.execute(
            select(actors.c.id, actors.c.type)
            .join(
                parties,
                (parties.c.org_id == actors.c.org_id)
                & (parties.c.created_by_actor_id == actors.c.id),
            )
            .where(parties.c.id == party["id"])
        ).one() == (device["id"], "DEVICE")


def test_user_cannot_link_to_nonhuman_or_be_retyped(tenants, app_connection, migrator_connection):
    a, _ = tenants
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        device = create_actor(
            app_connection,
            org_id=a.org_id,
            performer_id=a.ids["actors"],
            actor_type=ActorType.DEVICE,
            display_name="Not a tiny employee",
        )
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(
                users.insert().values(
                    id=uuid7(),
                    org_id=a.org_id,
                    actor_id=device["id"],
                    username="not-human",
                    password_hash=hash_password(a.password),
                )
            )
    assert error.value.orig.sqlstate == "23503"
    assert error.value.orig.diag.constraint_name == "fk_users_human_actor"
    with pytest.raises(DBAPIError) as error:
        migrator_connection.execute(
            actors.update().where(actors.c.id == a.ids["actors"]).values(type="DEVICE")
        )
    assert error.value.orig.sqlstate == "23503"
    migrator_connection.rollback()


def test_party_role_set_rejects_duplicates_and_invalid_values(tenants, app_connection):
    a, _ = tenants
    for role, state in (("INTERNAL", "23505"), ("ERP_ADMIN", "23514")):
        with pytest.raises(DBAPIError) as error:
            with app_connection.begin():
                set_organization(app_connection, a.org_id)
                app_connection.execute(
                    party_roles.insert().values(
                        id=uuid7(),
                        org_id=a.org_id,
                        party_id=a.ids["parties"],
                        role=role,
                    )
                )
        assert error.value.orig.sqlstate == state


def test_rls_helper_restores_grants_and_resists_permissive_policy(
    tenants,
    migrator_connection,
    app_connection,
):
    a, b = tenants
    table = parties
    try:
        migrator_connection.exec_driver_sql("GRANT ALL ON fleetops.parties TO fleetops_app")
        migrator_connection.exec_driver_sql(
            "GRANT UPDATE (display_name) ON fleetops.parties TO PUBLIC"
        )
        for _ in range(2):
            apply_tenant_policy(migrator_connection, table)
        migrator_connection.exec_driver_sql(
            "CREATE POLICY unrelated_allow ON fleetops.parties TO fleetops_app "
            "USING (true) WITH CHECK (true)"
        )
        migrator_connection.commit()
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            assert (
                app_connection.execute(
                    select(parties).where(parties.c.id == b.ids["parties"])
                ).all()
                == []
            )
            assert (
                app_connection.exec_driver_sql(
                    "SELECT has_column_privilege(current_user, 'fleetops.parties', "
                    "'display_name', 'UPDATE')"
                ).scalar_one()
                is False
            )
            assert (
                app_connection.exec_driver_sql(
                    "SELECT has_table_privilege(current_user, 'fleetops.parties', 'TRUNCATE')"
                ).scalar_one()
                is False
            )
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "DROP POLICY IF EXISTS unrelated_allow ON fleetops.parties"
        )
        apply_tenant_policy(migrator_connection, table)
        migrator_connection.commit()


def test_policy_helper_revokes_columns_missing_from_metadata(
    migrator_connection,
    app_connection,
):
    # A stale Table object must not leave a real column ACL behind after REVOKE ALL.
    name = "policy_probe_" + uuid7().hex
    qualified = "fleetops." + name
    migrator_connection.exec_driver_sql(
        f"CREATE TABLE {qualified} (id uuid, org_id uuid, stale_column text)"
    )
    migrator_connection.exec_driver_sql(
        f"GRANT UPDATE (stale_column) ON {qualified} TO PUBLIC, fleetops_app"
    )
    partial = Table(name, MetaData(schema="fleetops"), Column("id", Uuid), Column("org_id", Uuid))
    try:
        apply_tenant_policy(migrator_connection, partial)
        migrator_connection.commit()
        assert (
            app_connection.exec_driver_sql(
                "SELECT has_column_privilege(current_user, %s, 'stale_column', 'UPDATE')",
                (qualified,),
            ).scalar_one()
            is False
        )
        with pytest.raises(RuntimeError):
            apply_tenant_policy(app_connection, partial)
        with pytest.raises(ValueError):
            apply_tenant_policy(migrator_connection, partial, privileges=("TRUNCATE",))
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(f"DROP TABLE {qualified}")
        migrator_connection.commit()
