"""Adversarial persisted ancestry must terminate and never masquerade as a complete path."""

import pytest
from server.tests.auth_context import set_authenticated
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import locations
from fleetops.domain.space import SpaceHierarchyInvalid, SpaceNotFound, location_path


def test_space_path_without_context_is_not_found(space_data, app_connection):
    with app_connection.begin():
        with pytest.raises(SpaceNotFound):
            location_path(app_connection, space_data[0].location_id)


def test_space_path_detects_persisted_cycle(
    space_data, space_values, migrator_connection, app_connection, space_client
):
    # Only the owner disables the guard to seed corruption. Re-enable it before
    # runtime reads, proving the resolver retains its own termination defense.
    a, _ = space_data
    first, second = uuid7(), uuid7()
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.locations DISABLE TRIGGER ck_locations_acyclic"
    )
    try:
        migrator_connection.execute(
            locations.insert().values(
                [
                    space_values(a, locations, id=first, code="CYCLE-1", parent_location_id=second),
                    space_values(a, locations, id=second, code="CYCLE-2", parent_location_id=first),
                ]
            ),
        )
        migrator_connection.commit()
    finally:
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "ALTER TABLE fleetops.locations ENABLE TRIGGER ck_locations_acyclic"
        )
        migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.exec_driver_sql("SET LOCAL statement_timeout = '2s'")
        with pytest.raises(SpaceHierarchyInvalid):
            location_path(app_connection, first)
    response = space_client.get(
        f"/locations/{first}/path", headers={"Authorization": f"Bearer {a.raw_token}"}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "Location ancestry is cyclic or incomplete"}


@pytest.mark.parametrize("malformation", ["missing", "cross-facility", "cross-org"])
def test_space_path_rejects_incomplete_ancestry(
    malformation, space_data, migrator_connection, app_connection, space_client
):
    # Drop only this FK inside the disposable test DB to simulate legacy/corrupt data.
    # Restore both the row and the exact constraint in finally; production keeps the FK.
    a, b = space_data
    parent = {
        "missing": uuid7(),
        "cross-facility": a.other_location_id,
        "cross-org": b.location_id,
    }[malformation]
    definition = migrator_connection.exec_driver_sql(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid='fleetops.locations'::regclass AND conname='fk_locations_parent'"
    ).scalar_one()
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.locations DROP CONSTRAINT fk_locations_parent"
    )
    migrator_connection.execute(
        locations.update().where(locations.c.id == a.location_id).values(parent_location_id=parent)
    )
    migrator_connection.commit()
    try:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            with pytest.raises(SpaceHierarchyInvalid):
                location_path(app_connection, a.location_id)
        response = space_client.get(
            f"/locations/{a.location_id}/path", headers={"Authorization": f"Bearer {a.raw_token}"}
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "Location ancestry is cyclic or incomplete"}
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.execute(
            locations.update()
            .where(locations.c.id == a.location_id)
            .values(parent_location_id=None)
        )
        migrator_connection.exec_driver_sql(
            f"ALTER TABLE fleetops.locations ADD CONSTRAINT fk_locations_parent {definition}"
        )
        migrator_connection.commit()


def test_space_path_has_no_arbitrary_depth_truncation(
    space_data, space_values, migrator_connection, app_connection
):
    a, _ = space_data
    parent = a.location_id
    expected = [parent]
    for depth in range(100):
        values = space_values(a, locations, code=f"DEPTH-{depth}", parent_location_id=parent)
        migrator_connection.execute(locations.insert().values(**values))
        parent = values["id"]
        expected.append(parent)
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert [row["id"] for row in location_path(app_connection, parent)] == expected
        assert (
            app_connection.execute(
                select(locations.c.id).where(locations.c.id == parent)
            ).scalar_one()
            == parent
        )
