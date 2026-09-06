"""HTTP acceptance for catalog identity, reference cardinality, and authenticated attribution."""

from uuid import UUID

import pytest
from sqlalchemy import select
from uuid6 import uuid7

from fleetops.db.metadata import external_references, items
from fleetops.domain.catalog import REFERENCE_TARGETS

REFERENCE = {"system": "DigiKey", "reference_type": "DISTRIBUTOR_PART", "external_value": "123-456"}


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def item_body(tenant, **changes):
    return {
        "manufacturer_party_id": str(tenant.party_id),
        "manufacturer_part_number": "API-100",
        "description": "API catalog item",
        "revision": None,
        "uom": "EA",
        "serialized": True,
        **changes,
    }


def persisted_catalog(connection):
    """Owner snapshots only inspect effects; the HTTP request itself uses fleetops_app."""
    result = [
        connection.execute(select(table).order_by(table.c.id)).all()
        for table in (items, external_references)
    ]
    connection.rollback()
    return result


def test_catalog_api_create_get_update_and_deactivate_preserve_identity(
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, _ = catalog_data
    response = catalog_client.post(
        "/items", json=item_body(a, export_controlled=True), headers=headers(a)
    )
    assert response.status_code == 201, response.text
    created = response.json()
    item_id = created["id"]
    assert UUID(item_id).version == 7
    assert created["org_id"] == str(a.org_id)
    assert created["created_by_actor_id"] == created["updated_by_actor_id"] == str(a.actor_id)
    assert created["export_controlled"] is True and created["export_classification"] is None
    assert created["created_at"] and created["updated_at"]
    assert catalog_client.get(f"/items/{item_id}", headers=headers(a)).json() == created
    assert {row["id"] for row in catalog_client.get("/items", headers=headers(a)).json()} == {
        item_id,
        str(a.item_id),
    }
    attached = catalog_client.post(
        "/external-references",
        headers=headers(a),
        json={
            **REFERENCE,
            "entity_type": "ITEM",
            "entity_id": item_id,
        },
    )
    assert attached.status_code == 201
    before_refs = migrator_connection.execute(select(external_references)).all()
    migrator_connection.rollback()
    update = catalog_client.patch(
        f"/items/{item_id}",
        headers=headers(a),
        json={
            "uom": "M",
            "description": "New default",
            "manufacturer_part_number": "RENAMED",
            "export_classification": "3A001",
            "revision": "B",
            "serialized": False,
        },
    )
    assert update.status_code == 200, update.text
    changed = update.json()
    assert changed["id"] == item_id and changed["uom"] == "M"
    assert changed["created_at"] == created["created_at"]
    assert changed["created_by_actor_id"] == created["created_by_actor_id"]
    assert changed["updated_at"] >= created["updated_at"]
    assert changed["manufacturer_part_number"] == "RENAMED"
    assert changed["description"] == "New default" and changed["serialized"] is False
    assert migrator_connection.execute(select(external_references)).all() == before_refs
    migrator_connection.rollback()
    cleared = catalog_client.patch(
        f"/items/{item_id}",
        headers=headers(a),
        json={
            "revision": None,
            "export_classification": None,
        },
    )
    assert cleared.status_code == 200
    assert cleared.json()["revision"] is None and cleared.json()["export_classification"] is None
    assert catalog_client.delete(f"/items/{item_id}", headers=headers(a)).status_code == 204
    final = catalog_client.get(f"/items/{item_id}", headers=headers(a)).json()
    assert final["id"] == item_id and final["active"] is False
    assert catalog_client.get(
        "/external-references",
        headers=headers(a),
        params={
            "entity_type": "ITEM",
            "entity_id": item_id,
        },
    ).json() == [attached.json()]


def test_catalog_shared_reference_search_returns_all_ids_and_supports_party(
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, b = catalog_data
    second = catalog_client.post(
        "/items",
        headers=headers(a),
        json=item_body(
            a,
            manufacturer_part_number="CAT-100",
            revision="B",
        ),
    )
    assert second.status_code == 201
    second_id = second.json()["id"]
    attached = catalog_client.post(
        "/external-references",
        headers=headers(a),
        json={
            **REFERENCE,
            "entity_type": "ITEM",
            "entity_id": second_id,
        },
    )
    assert attached.status_code == 201
    assert UUID(attached.json()["id"]).version == 7
    assert attached.json()["created_by_actor_id"] == str(a.actor_id)
    before = persisted_catalog(migrator_connection)
    duplicate = catalog_client.post(
        "/external-references",
        headers=headers(a),
        json={
            **REFERENCE,
            "entity_type": "ITEM",
            "entity_id": second_id,
        },
    )
    assert duplicate.status_code == 409
    assert persisted_catalog(migrator_connection) == before
    found = catalog_client.get("/external-references/search", headers=headers(a), params=REFERENCE)
    assert found.status_code == 200
    assert {row["entity_id"] for row in found.json()} == {str(a.item_id), second_id}
    assert len(found.json()) == 2
    assert all(
        row["entity_type"] == "ITEM" and row["org_id"] == str(a.org_id) for row in found.json()
    )
    assert str(b.item_id) not in {row["entity_id"] for row in found.json()}
    # Search cardinality remains explicit for zero and one matches, too.
    assert (
        catalog_client.get(
            "/external-references/search",
            headers=headers(a),
            params={
                **REFERENCE,
                "external_value": "no-match",
            },
        ).json()
        == []
    )
    assert [
        row["entity_id"]
        for row in catalog_client.get(
            "/external-references/search",
            headers=headers(b),
            params=REFERENCE,
        ).json()
    ] == [str(b.item_id)]
    for changed_key in ("system", "reference_type"):
        assert (
            catalog_client.get(
                "/external-references/search",
                headers=headers(a),
                params={
                    **REFERENCE,
                    changed_key: "different-namespace",
                },
            ).json()
            == []
        )
    assert set(REFERENCE_TARGETS) == {"ITEM", "PARTY"}
    party_ref = catalog_client.post(
        "/external-references",
        headers=headers(a),
        json={
            **REFERENCE,
            "entity_type": "PARTY",
            "entity_id": str(a.party_id),
        },
    )
    assert party_ref.status_code == 201
    assert catalog_client.get(
        "/external-references",
        headers=headers(a),
        params={
            "entity_type": "PARTY",
            "entity_id": str(a.party_id),
        },
    ).json() == [party_ref.json()]
    assert (
        len(
            catalog_client.get(
                "/external-references/search",
                headers=headers(a),
                params=REFERENCE,
            ).json()
        )
        == 3
    )


@pytest.mark.parametrize("kind", ["ITEM", "PARTY"])
@pytest.mark.parametrize("target", ["missing", "cross-org"])
def test_catalog_reference_target_must_exist_and_be_visible(
    kind,
    target,
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, b = catalog_data
    target_id = uuid7() if target == "missing" else (b.item_id if kind == "ITEM" else b.party_id)
    before = persisted_catalog(migrator_connection)
    assert (
        catalog_client.post(
            "/external-references",
            headers=headers(a),
            json={
                **REFERENCE,
                "entity_type": kind,
                "entity_id": str(target_id),
            },
        ).status_code
        == 404
    )
    assert (
        catalog_client.get(
            "/external-references",
            headers=headers(a),
            params={
                "entity_type": kind,
                "entity_id": str(target_id),
            },
        ).status_code
        == 404
    )
    assert persisted_catalog(migrator_connection) == before


@pytest.mark.parametrize("kind", ["USER", "SESSION", "ACTOR", "ORGANIZATION", "ASSET"])
def test_catalog_api_rejects_unsupported_reference_types(
    kind,
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, _ = catalog_data
    before = persisted_catalog(migrator_connection)
    assert (
        catalog_client.post(
            "/external-references",
            headers=headers(a),
            json={
                **REFERENCE,
                "entity_type": kind,
                "entity_id": str(a.item_id),
            },
        ).status_code
        == 422
    )
    assert persisted_catalog(migrator_connection) == before


@pytest.mark.parametrize("target", ["vendor-only", "cross-org"])
def test_catalog_api_manufacturer_error_rolls_back(
    target,
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, b = catalog_data
    manufacturer = a.vendor_id if target == "vendor-only" else b.party_id
    before = persisted_catalog(migrator_connection)
    assert (
        catalog_client.post(
            "/items",
            headers=headers(a),
            json=item_body(
                a,
                manufacturer_party_id=str(manufacturer),
            ),
        ).status_code
        == 422
    )
    assert (
        catalog_client.patch(
            f"/items/{a.item_id}",
            headers=headers(a),
            json={
                "manufacturer_party_id": str(manufacturer),
            },
        ).status_code
        == 422
    )
    assert persisted_catalog(migrator_connection) == before


def test_catalog_api_duplicate_and_invalid_patch_leave_original_unchanged(
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, _ = catalog_data
    assert (
        catalog_client.post(
            "/items",
            headers=headers(a),
            json=item_body(
                a,
                manufacturer_part_number="CAT-100",
                revision="A",
            ),
        ).status_code
        == 409
    )
    first = catalog_client.post("/items", headers=headers(a), json=item_body(a))
    assert first.status_code == 201
    before = persisted_catalog(migrator_connection)
    assert catalog_client.post("/items", headers=headers(a), json=item_body(a)).status_code == 409
    assert (
        catalog_client.patch(
            f"/items/{first.json()['id']}",
            headers=headers(a),
            json={
                "manufacturer_part_number": "CAT-100",
                "revision": "A",
            },
        ).status_code
        == 409
    )
    for field in ("uom", "description", "manufacturer_party_id", "active"):
        assert (
            catalog_client.patch(
                f"/items/{a.item_id}",
                headers=headers(a),
                json={
                    field: None,
                },
            ).status_code
            == 422
        ), field
    assert (
        catalog_client.patch(
            f"/items/{a.item_id}", headers=headers(a), json={"uom": "REEL"}
        ).status_code
        == 422
    )
    assert persisted_catalog(migrator_connection) == before


@pytest.mark.parametrize("target", ["missing", "cross-org"])
def test_catalog_api_item_access_is_scoped(
    target,
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, b = catalog_data
    item_id = uuid7() if target == "missing" else b.item_id
    before = persisted_catalog(migrator_connection)
    assert catalog_client.get(f"/items/{item_id}", headers=headers(a)).status_code == 404
    assert (
        catalog_client.patch(
            f"/items/{item_id}", headers=headers(a), json={"active": False}
        ).status_code
        == 404
    )
    assert catalog_client.delete(f"/items/{item_id}", headers=headers(a)).status_code == 404
    assert persisted_catalog(migrator_connection) == before


@pytest.mark.parametrize("credential", [None, "invalid"])
@pytest.mark.parametrize(
    "route", ["create", "list", "get", "update", "deactivate", "attach", "references", "search"]
)
def test_catalog_every_route_requires_a_valid_session(
    route,
    credential,
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, _ = catalog_data
    method, path, body, params = {
        "create": ("POST", "/items", item_body(a), None),
        "list": ("GET", "/items", None, None),
        "get": ("GET", f"/items/{a.item_id}", None, None),
        "update": ("PATCH", f"/items/{a.item_id}", {"active": False}, None),
        "deactivate": ("DELETE", f"/items/{a.item_id}", None, None),
        "attach": (
            "POST",
            "/external-references",
            {
                **REFERENCE,
                "entity_type": "ITEM",
                "entity_id": str(a.item_id),
            },
            None,
        ),
        "references": (
            "GET",
            "/external-references",
            None,
            {
                "entity_type": "ITEM",
                "entity_id": str(a.item_id),
            },
        ),
        "search": ("GET", "/external-references/search", None, REFERENCE),
    }[route]
    before = persisted_catalog(migrator_connection)
    response = catalog_client.request(
        method,
        path,
        json=body,
        params=params,
        headers={} if credential is None else {"Authorization": "Bearer invalid"},
    )
    assert response.status_code == 401
    assert persisted_catalog(migrator_connection) == before


def test_catalog_spoofed_fields_cannot_choose_tenant_or_performer(
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, b = catalog_data
    before = persisted_catalog(migrator_connection)
    for field in (
        "org_id",
        "actor_id",
        "created_by_actor_id",
        "updated_by_actor_id",
        "id",
        "created_at",
        "updated_at",
    ):
        spoof = str(b.org_id if field == "org_id" else b.actor_id)
        assert (
            catalog_client.post(
                "/items",
                headers=headers(a),
                json={
                    **item_body(a),
                    field: spoof,
                },
            ).status_code
            == 422
        ), field
        assert (
            catalog_client.patch(
                f"/items/{a.item_id}",
                headers=headers(a),
                json={
                    field: spoof,
                },
            ).status_code
            == 422
        ), field
        assert (
            catalog_client.post(
                "/external-references",
                headers=headers(a),
                json={
                    **REFERENCE,
                    "entity_type": "ITEM",
                    "entity_id": str(a.item_id),
                    field: spoof,
                },
            ).status_code
            == 422
        ), field
    assert persisted_catalog(migrator_connection) == before
    spoof_headers = {**headers(a), "X-Org-ID": str(b.org_id), "X-Actor-ID": str(b.actor_id)}
    spoof_query = {"org_id": str(b.org_id), "actor_id": str(b.actor_id)}
    created = catalog_client.post(
        "/items", headers=spoof_headers, params=spoof_query, json=item_body(a)
    )
    assert created.status_code == 201
    assert created.json()["org_id"] == str(a.org_id)
    assert created.json()["created_by_actor_id"] == str(a.actor_id)
    changed = catalog_client.patch(
        f"/items/{a.item_id}",
        headers=spoof_headers,
        params=spoof_query,
        json={
            "description": "Authenticated change",
        },
    )
    assert changed.status_code == 200
    assert changed.json()["org_id"] == str(a.org_id)
    assert changed.json()["updated_by_actor_id"] == str(a.actor_id)
    reference = catalog_client.post(
        "/external-references",
        headers=spoof_headers,
        params=spoof_query,
        json={
            **REFERENCE,
            "entity_type": "PARTY",
            "entity_id": str(a.party_id),
        },
    )
    assert reference.status_code == 201
    assert reference.json()["org_id"] == str(a.org_id)
    assert reference.json()["created_by_actor_id"] == str(a.actor_id)
    searched = catalog_client.get(
        "/external-references/search",
        headers=spoof_headers,
        params={**REFERENCE, **spoof_query},
    )
    assert all(row["org_id"] == str(a.org_id) for row in searched.json())


def test_catalog_external_values_never_address_a_write_or_get_by_id(
    catalog_client,
    catalog_data,
    migrator_connection,
):
    a, _ = catalog_data
    before = persisted_catalog(migrator_connection)
    # A lookup value is deliberately not a UUID. No path may reinterpret it as one.
    assert catalog_client.get("/items/123-456", headers=headers(a)).status_code == 422
    assert (
        catalog_client.patch(
            "/items/123-456", headers=headers(a), json={"active": False}
        ).status_code
        == 422
    )
    assert catalog_client.delete("/items/123-456", headers=headers(a)).status_code == 422
    for body in (
        REFERENCE,
        {**REFERENCE, "entity_type": "ITEM"},
        {**REFERENCE, "entity_id": str(a.item_id)},
    ):
        assert (
            catalog_client.post("/external-references", headers=headers(a), json=body).status_code
            == 422
        )
    assert (
        catalog_client.patch(
            "/items", headers=headers(a), json={**REFERENCE, "active": False}
        ).status_code
        == 405
    )
    assert (
        catalog_client.patch(
            "/external-references/search",
            headers=headers(a),
            params=REFERENCE,
            json={"active": False},
        ).status_code
        == 405
    )
    assert (
        catalog_client.get("/external-references", headers=headers(a), params=REFERENCE).status_code
        == 422
    )
    assert persisted_catalog(migrator_connection) == before


def test_catalog_update_records_the_authenticated_performer_not_the_original_creator(
    catalog_client,
    catalog_data,
    migrator_connection,
    catalog_password_hash,
):
    import secrets
    from datetime import UTC, datetime, timedelta

    from fleetops.auth import token_digest
    from fleetops.db.metadata import actors, sessions, users

    a, _ = catalog_data
    performer, user_id, raw_token = uuid7(), uuid7(), secrets.token_urlsafe(32)
    migrator_connection.execute(
        actors.insert().values(
            id=performer,
            org_id=a.org_id,
            type="HUMAN",
            display_name="Second operator",
            created_by_actor_id=a.actor_id,
        )
    )
    migrator_connection.execute(
        users.insert().values(
            id=user_id,
            org_id=a.org_id,
            actor_id=performer,
            username="second-operator",
            password_hash=catalog_password_hash,
        )
    )
    migrator_connection.execute(
        sessions.insert().values(
            id=uuid7(),
            org_id=a.org_id,
            user_id=user_id,
            token_digest=token_digest(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    migrator_connection.commit()
    changed = catalog_client.patch(
        f"/items/{a.item_id}",
        headers={"Authorization": f"Bearer {raw_token}"},
        json={"description": "Changed by another authenticated human"},
    )
    assert changed.status_code == 200
    assert changed.json()["created_by_actor_id"] == str(a.actor_id)
    assert changed.json()["updated_by_actor_id"] == str(performer)
    assert changed.json()["org_id"] == str(a.org_id)
