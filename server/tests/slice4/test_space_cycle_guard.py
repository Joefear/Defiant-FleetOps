"""Durable cycle rejection must cover direct runtime SQL, not merely the create API."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from fleetops.db.metadata import locations
from fleetops.db.tenancy import set_organization


@pytest.mark.parametrize("size", [2, 3, 12], ids=["two-node", "three-node", "long-cycle"])
def test_cycle_guard_rejects_one_statement_atomically(
    size, space_data, space_values, app_connection, migrator_connection
):
    a, _ = space_data
    ids = [uuid7() for _ in range(size)]
    rows = [
        space_values(
            a,
            locations,
            id=row_id,
            code=f"CYCLE-{index}",
            parent_location_id=ids[(index + 1) % size],
        )
        for index, row_id in enumerate(ids)
    ]
    # A valid sibling in the same statement must roll back with the cycle.
    sibling = space_values(a, locations, code="VALID-SIBLING", parent_location_id=a.location_id)
    rows.insert(0, sibling)
    statement = locations.insert().values(rows).returning(locations.c.id)
    print(statement.compile(dialect=app_connection.dialect, compile_kwargs={"literal_binds": True}))
    failure = None
    try:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            assert app_connection.exec_driver_sql("SELECT current_user, session_user").one() == (
                "fleetops_app",
                "fleetops_app",
            )
            app_connection.exec_driver_sql("SET LOCAL statement_timeout = '3s'")
            accepted = app_connection.execute(statement).scalars().all()
            print(f"ACCEPTED {len(accepted)} rows as fleetops_app; transaction committed")
    except IntegrityError as error:
        failure = error
    persisted = (
        migrator_connection.execute(
            select(locations.c.id).where(locations.c.id.in_(ids + [sibling["id"]]))
        )
        .scalars()
        .all()
    )
    migrator_connection.rollback()
    print(f"Committed rows visible from independent owner connection: {len(persisted)}")
    assert failure is not None, "PostgreSQL accepted and committed a location cycle"
    assert failure.orig.sqlstate == "23514"
    assert failure.orig.diag.constraint_name == "ck_locations_acyclic"
    assert failure.orig.diag.message_primary == "Location hierarchy cycle is not permitted"
    assert persisted == []


@pytest.mark.parametrize("reverse", [False, True], ids=["parent-first", "child-first"])
def test_cycle_guard_allows_complete_multirow_hierarchy(
    reverse, space_data, space_values, app_connection, migrator_connection
):
    from fleetops.domain.space import location_path

    a, _ = space_data
    ids = [uuid7() for _ in range(4)]
    rows = [
        space_values(
            a,
            locations,
            id=row_id,
            code=f"TREE-{index}",
            parent_location_id=ids[index - 1] if index else None,
        )
        for index, row_id in enumerate(ids)
    ]
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        accepted = (
            app_connection.execute(
                locations.insert()
                .values(list(reversed(rows)) if reverse else rows)
                .returning(locations.c.id)
            )
            .scalars()
            .all()
        )
        assert set(accepted) == set(ids)
        assert [row["id"] for row in location_path(app_connection, ids[-1])] == ids
    assert set(
        migrator_connection.execute(select(locations.c.id).where(locations.c.id.in_(ids))).scalars()
    ) == set(ids)


def test_cycle_guard_allows_incremental_root_child_grandchild(
    space_data, space_values, app_connection
):
    from fleetops.domain.space import location_path

    a, _ = space_data
    expected = []
    for index in range(3):
        values = space_values(
            a,
            locations,
            code=f"CHAIN-{index}",
            parent_location_id=expected[-1] if expected else None,
        )
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(locations.insert().values(**values))
        expected.append(values["id"])
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        assert [row["id"] for row in location_path(app_connection, expected[-1])] == expected


@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE fleetops.locations DISABLE TRIGGER ck_locations_acyclic",
        "DROP TRIGGER ck_locations_acyclic ON fleetops.locations",
        "SET LOCAL session_replication_role = replica",
        "SELECT fleetops.enforce_location_acyclic()",
    ],
)
def test_cycle_guard_runtime_cannot_bypass_guard(statement, app_connection, assert_denied):
    assert_denied(app_connection, statement)


def test_cycle_guard_is_invoker_immediate_and_has_no_runtime_execute(
    migrator_connection, app_connection
):
    function = migrator_connection.exec_driver_sql("""
        SELECT pg_get_userbyid(proowner), prosecdef, provolatile,
               prorettype::regtype::text, proconfig
        FROM pg_proc WHERE oid='fleetops.enforce_location_acyclic()'::regprocedure
    """).one()
    assert function == (
        "fleetops_migrator",
        False,
        "v",
        "trigger",
        ["search_path=pg_catalog, pg_temp"],
    )
    trigger = migrator_connection.exec_driver_sql("""
        SELECT tgtype, tgenabled, tgdeferrable, tginitdeferred, tgisinternal,
               tgfoid::regprocedure::text
        FROM pg_trigger WHERE tgrelid='fleetops.locations'::regclass
          AND tgname='ck_locations_acyclic'
    """).one()
    assert trigger == (5, "O", False, False, False, "enforce_location_acyclic()")
    assert migrator_connection.exec_driver_sql("""
        SELECT contype, condeferrable, condeferred FROM pg_constraint
        WHERE conrelid='fleetops.locations'::regclass AND conname='ck_locations_acyclic'
    """).one() == ("t", False, False)
    assert (
        app_connection.exec_driver_sql(
            "SELECT has_function_privilege(current_user, "
            "'fleetops.enforce_location_acyclic()', 'EXECUTE')"
        ).scalar_one()
        is False
    )
    assert (
        migrator_connection.exec_driver_sql("""
        SELECT count(*) FROM pg_proc p, LATERAL aclexplode(p.proacl) acl
        WHERE p.oid='fleetops.enforce_location_acyclic()'::regprocedure AND acl.grantee=0
    """).scalar_one()
        == 0
    )
