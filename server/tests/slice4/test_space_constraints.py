"""Direct PostgreSQL proofs for tenant/facility relationships and location vocabulary."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from fleetops.db.metadata import facilities, locations
from fleetops.db.tenancy import set_organization
from fleetops.domain.location_kinds import LocationKind


@pytest.mark.parametrize("kind", [kind.value for kind in LocationKind])
def test_space_database_accepts_all_eight_kinds(kind, space_data, space_values, app_connection):
    a, _ = space_data
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        row = (
            app_connection.execute(
                locations.insert()
                .values(**space_values(a, locations, kind=kind))
                .returning(locations)
            )
            .mappings()
            .one()
        )
        assert row["kind"] == kind and row["parent_location_id"] is None


@pytest.mark.parametrize(
    ("case", "state", "constraint"),
    [
        ("self-parent", "23514", "ck_locations_not_self_parent"),
        ("cross-org-facility", "23503", "fk_locations_facility"),
        ("missing-facility", "23503", "fk_locations_facility"),
        ("cross-facility-parent", "23503", "fk_locations_parent"),
        ("cross-org-parent", "23503", "fk_locations_parent"),
        ("missing-parent", "23503", "fk_locations_parent"),
        ("invalid-kind", "23514", "ck_locations_kind"),
        ("lowercase-kind", "23514", "ck_locations_kind"),
        ("duplicate-code", "23505", "uq_locations_facility_code"),
        ("cross-org-creator", "23503", "fk_locations_creator"),
        ("blank-code", "23514", "ck_locations_code"),
    ],
)
def test_space_database_rejects_invalid_location(
    case, state, constraint, space_data, space_values, app_connection, migrator_connection
):
    a, b = space_data
    values = space_values(a, locations)
    changes = {
        "self-parent": {"parent_location_id": values["id"]},
        "cross-org-facility": {"facility_id": b.facility_id},
        "missing-facility": {"facility_id": values["id"]},
        "cross-facility-parent": {"parent_location_id": a.other_location_id},
        "cross-org-parent": {"parent_location_id": b.location_id},
        "missing-parent": {"parent_location_id": values["id"], "id": b.user_id},
        "invalid-kind": {"kind": "WAREHOUSE"},
        "lowercase-kind": {"kind": "bin"},
        "duplicate-code": {"code": "SITE"},
        "cross-org-creator": {"created_by_actor_id": b.actor_id},
        "blank-code": {"code": "  "},
    }
    values.update(changes[case])
    with pytest.raises(IntegrityError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(locations.insert().values(**values))
    assert error.value.orig.sqlstate == state
    assert error.value.orig.diag.constraint_name == constraint
    assert (
        migrator_connection.execute(select(locations).where(locations.c.id == values["id"])).all()
        == []
    )


def test_space_database_allows_facility_local_codes_and_preserves_case(
    space_data, space_values, app_connection
):
    a, _ = space_data
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        for facility_id, code in (
            (a.facility_id, "RACK-01"),
            (a.other_facility_id, "RACK-01"),
            (a.facility_id, "rack-01"),
        ):
            row = (
                app_connection.execute(
                    locations.insert()
                    .values(**space_values(a, locations, facility_id=facility_id, code=code))
                    .returning(locations)
                )
                .mappings()
                .one()
            )
            assert row["facility_id"] == facility_id and row["code"] == code


def test_space_database_allows_child_in_same_facility(space_data, space_values, app_connection):
    a, _ = space_data
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        assert (
            app_connection.execute(
                locations.insert()
                .values(**space_values(a, locations, parent_location_id=a.location_id))
                .returning(locations.c.parent_location_id)
            ).scalar_one()
            == a.location_id
        )


@pytest.mark.parametrize("case", ["cross-org-creator", "missing-org", "blank-name"])
def test_space_database_rejects_invalid_facility(
    case, space_data, space_values, migrator_connection
):
    # Owner writes deliberately bypass RLS to prove that the tenant/actor FKs remain durable.
    a, b = space_data
    values = space_values(a, facilities)
    changes, constraint = {
        "cross-org-creator": ({"created_by_actor_id": b.actor_id}, "fk_facilities_creator"),
        "missing-org": ({"org_id": values["id"]}, "fk_facilities_org"),
        "blank-name": ({"name": " "}, "ck_facilities_name"),
    }[case]
    values.update(changes)
    with pytest.raises(IntegrityError) as error:
        migrator_connection.execute(facilities.insert().values(**values))
    assert error.value.orig.diag.constraint_name == constraint
    migrator_connection.rollback()
