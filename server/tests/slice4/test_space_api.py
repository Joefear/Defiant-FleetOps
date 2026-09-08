"""Authenticated space behavior against persisted PostgreSQL rows, including rejected writes."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from server.tests.auth_context import set_authenticated
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import facilities, locations
from fleetops.domain.space import SpaceInvalid, create_facility


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def location_body(tenant, **changes):
    return {
        "facility_id": str(tenant.facility_id),
        "code": "NEW",
        "name": "New location",
        "kind": "BIN",
        **changes,
    }


def space_rows(connection):
    """Snapshot both tables as owner to prove rejected requests have no side effects."""
    rows = {
        table.name: connection.execute(select(table).order_by(table.c.id)).all()
        for table in (facilities, locations)
    }
    connection.rollback()
    return rows


def test_space_facility_uuid_attribution_timezone_and_server_time(
    space_data, space_client, migrator_connection
):
    a, _ = space_data
    # Compare database-clock bounds; the Docker VM and host clocks can differ.
    before = migrator_connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    migrator_connection.rollback()
    response = space_client.post(
        "/facilities",
        headers=headers(a),
        json={
            "name": "  Chicago facility  ",
            "timezone": " America/Chicago ",
            "active": False,
        },
    )
    assert response.status_code == 201, response.text
    row = response.json()
    assert UUID(row["id"]).version == 7
    assert row["org_id"] == str(a.org_id) and row["created_by_actor_id"] == str(a.actor_id)
    assert row["name"] == "Chicago facility" and row["active"] is False
    assert ZoneInfo(row["timezone"]).key == "America/Chicago"
    recorded = datetime.fromisoformat(row["created_at"])
    assert recorded.utcoffset().total_seconds() == 0
    after = migrator_connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
    assert before <= recorded <= after
    stored = (
        migrator_connection.execute(select(facilities).where(facilities.c.id == UUID(row["id"])))
        .mappings()
        .one()
    )
    assert stored["created_at"] == recorded and stored["timezone"] == "America/Chicago"
    listed = space_client.get("/facilities", headers=headers(a))
    assert listed.status_code == 200 and row in listed.json()


@pytest.mark.parametrize(
    "timezone",
    [
        "Not/A_Zone",
        "america/chicago",
        "/etc/localtime",
        "../UTC",
        "UTC+05:00",
        "posix/America/Chicago",
        "",
    ],
)
def test_space_invalid_timezone_writes_nothing(
    timezone, space_data, space_client, migrator_connection
):
    before = space_rows(migrator_connection)
    response = space_client.post(
        "/facilities",
        headers=headers(space_data[0]),
        json={"name": "Invalid", "timezone": timezone},
    )
    assert response.status_code == 422
    assert space_rows(migrator_connection) == before


def test_space_domain_also_validates_timezone_before_insert(
    space_data, app_connection, migrator_connection
):
    a, _ = space_data
    before = space_rows(migrator_connection)
    with app_connection.begin():
        set_authenticated(app_connection, a)
        with pytest.raises(SpaceInvalid, match="Unknown IANA"):
            create_facility(
                app_connection,
                org_id=a.org_id,
                performer_id=a.actor_id,
                values={"name": "Invalid", "timezone": "Mars/Olympus"},
            )
    assert space_rows(migrator_connection) == before


def test_space_root_child_and_persisted_root_to_bin_path(
    space_data, space_client, migrator_connection
):
    a, _ = space_data
    created = []
    parent = None
    for kind, code in (
        ("SITE", "SITE-A"),
        ("ROOM", "ROOM-101"),
        ("RACK", "RACK-03"),
        ("BIN", "BIN-07"),
    ):
        response = space_client.post(
            "/locations",
            headers=headers(a),
            json=location_body(
                a,
                kind=kind,
                code=f" {code} ",
                parent_location_id=parent,
                active=False,
            ),
        )
        assert response.status_code == 201, response.text
        row = response.json()
        assert UUID(row["id"]).version == 7
        assert row["code"] == code and row["parent_location_id"] == parent
        assert row["org_id"] == str(a.org_id) and row["created_by_actor_id"] == str(a.actor_id)
        assert row["active"] is False
        assert datetime.fromisoformat(row["created_at"]).utcoffset().total_seconds() == 0
        parent = row["id"]
        created.append(row)
    # An independent connection confirms the endpoint resolves committed rows.
    persisted = (
        migrator_connection.execute(
            select(locations.c.id).where(locations.c.id.in_([UUID(row["id"]) for row in created]))
        )
        .scalars()
        .all()
    )
    assert set(persisted) == {UUID(row["id"]) for row in created}
    migrator_connection.rollback()
    path = space_client.get(f"/locations/{parent}/path", headers=headers(a))
    assert path.status_code == 200 and path.json() == created
    assert space_client.get(f"/locations/{created[0]['id']}/path", headers=headers(a)).json() == [
        created[0]
    ]
    listed = space_client.get("/locations", headers=headers(a))
    assert listed.status_code == 200
    assert all(row in listed.json() for row in created)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-code",
        "cross-org-facility",
        "cross-facility-parent",
        "cross-org-parent",
        "missing-parent",
        "invalid-kind",
    ],
)
def test_space_api_rejects_invalid_location_without_write(
    case, space_data, space_client, migrator_connection
):
    a, b = space_data
    changes = {
        "duplicate-code": {"code": "SITE"},
        "cross-org-facility": {"facility_id": str(b.facility_id)},
        "cross-facility-parent": {"parent_location_id": str(a.other_location_id)},
        "cross-org-parent": {"parent_location_id": str(b.location_id)},
        "missing-parent": {"parent_location_id": str(uuid7())},
        "invalid-kind": {"kind": "WAREHOUSE"},
    }[case]
    before = space_rows(migrator_connection)
    response = space_client.post("/locations", headers=headers(a), json=location_body(a, **changes))
    assert response.status_code == (409 if case == "duplicate-code" else 422)
    assert space_rows(migrator_connection) == before


@pytest.mark.parametrize("route", ["/facilities", "/locations"])
@pytest.mark.parametrize(
    "field", ["org_id", "actor_id", "created_by_actor_id", "id", "created_at", "recorded_at"]
)
def test_space_api_rejects_spoofed_authority(
    route, field, space_data, space_client, migrator_connection
):
    a, b = space_data
    body = {"name": "Spoof", "timezone": "UTC"} if route == "/facilities" else location_body(a)
    body[field] = "2020-01-01T00:00:00Z" if field.endswith("_at") else str(b.actor_id)
    before = space_rows(migrator_connection)
    response = space_client.post(route, headers=headers(a), json=body)
    assert response.status_code == 422
    assert space_rows(migrator_connection) == before


@pytest.mark.parametrize("credential", ["missing", "invalid"])
@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("POST", "/facilities"),
        ("GET", "/facilities"),
        ("POST", "/locations"),
        ("GET", "/locations"),
        ("GET", "/locations/{id}/path"),
    ],
)
def test_space_all_routes_require_valid_bearer_and_write_nothing(
    credential, method, route, space_data, space_client, migrator_connection
):
    a, _ = space_data
    before = space_rows(migrator_connection)
    body = (
        {"name": "Unauthorized", "timezone": "UTC"} if route == "/facilities" else location_body(a)
    )
    response = space_client.request(
        method,
        route.format(id=a.location_id),
        headers={} if credential == "missing" else {"Authorization": "Bearer invalid"},
        **({"json": body} if method == "POST" else {}),
    )
    assert response.status_code == 401
    assert space_rows(migrator_connection) == before


def test_space_lists_and_path_do_not_expose_other_tenant(space_data, space_client):
    a, b = space_data
    for route in ("/facilities", "/locations"):
        response = space_client.get(route, headers=headers(a))
        assert response.status_code == 200 and len(response.json()) == 2
        assert all(row["org_id"] == str(a.org_id) for row in response.json())
    hidden = space_client.get(f"/locations/{b.location_id}/path", headers=headers(a))
    absent = space_client.get(f"/locations/{uuid7()}/path", headers=headers(a))
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json() == absent.json() == {"detail": "Location not found"}


@pytest.mark.parametrize("method", ["PATCH", "PUT", "DELETE"])
@pytest.mark.parametrize("route", ["/facilities", "/locations"])
def test_space_has_no_update_or_delete_surface(method, route, space_data, space_client):
    a, _ = space_data
    assert space_client.request(method, route, headers=headers(a)).status_code == 405
    assert (
        space_client.request(method, f"{route}/{a.location_id}", headers=headers(a)).status_code
        == 404
    )


def test_space_codes_preserve_case_and_allow_reuse_in_other_facilities(space_data, space_client):
    a, _ = space_data
    for facility_id, code in (
        (a.facility_id, "RACK-01"),
        (a.other_facility_id, "RACK-01"),
        (a.facility_id, "rack-01"),
    ):
        response = space_client.post(
            "/locations",
            headers=headers(a),
            json=location_body(a, facility_id=str(facility_id), code=code),
        )
        assert response.status_code == 201 and response.json()["code"] == code
