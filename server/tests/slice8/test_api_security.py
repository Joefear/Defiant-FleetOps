"""HTTP workflows and direct-SQL credential/tenant boundaries use the actual app role."""

from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice8.conftest import headers, line_values, order_values, snapshot, write
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import items
from fleetops.db.metadata import purchase_order_lines as lines
from fleetops.db.metadata import purchase_orders as orders
from fleetops.db.tenancy import set_organization


def test_complete_api_authoring_issuance_amendment_and_history(
    space_client,
    space_data,
    other_human,
    migrator_connection,
):
    client, a = space_client, space_data[0]
    auth, other_auth = headers(a), headers(other_human)
    result = client.post(
        "/purchase-orders",
        headers=auth,
        json={"vendor_party_id": str(a.vendor_id), "po_number": " PO-1 "},
    )
    assert result.status_code == 201, result.text
    po = result.json()
    assert po["status"] == "DRAFT" and po["po_number"] == "PO-1"
    assert po["issued_at"] is po["issued_by_actor_id"] is None
    assert UUID(po["id"]).version == 7
    base = f"/purchase-orders/{po['id']}"
    result = client.post(
        f"{base}/lines",
        headers=auth,
        json={
            "item_id": str(a.item_id),
            "line_number": 1,
            "quantity": "1.125",
            "unit_price": "0",
        },
    )
    assert result.status_code == 201, result.text
    line = result.json()
    assert line["uom"] == "EA" and line["active"] and not line["superseded"]
    result = client.patch(base, headers=other_auth, json={"notes": "Supplier expectation"})
    assert result.status_code == 200, result.text
    assert result.json()["updated_by_actor_id"] == str(other_human.actor_id)
    assert result.json()["created_by_actor_id"] == str(a.actor_id)
    line_path = f"{base}/lines/{line['id']}"
    result = client.patch(
        line_path, headers=other_auth, json={"quantity": "2.5", "unit_price": "1.123456789"}
    )
    assert result.status_code == 200, result.text
    assert result.json()["updated_by_actor_id"] == str(other_human.actor_id)
    assert result.json()["updated_at"] != line["updated_at"]
    issue = client.post(f"{base}/issue", headers=other_auth)
    assert issue.status_code == 200, issue.text
    assert issue.json()["status"] == "ISSUED"
    assert issue.json()["issued_by_actor_id"] == str(other_human.actor_id)
    assert issue.json()["issued_at"] == issue.json()["updated_at"]
    original = snapshot(migrator_connection, lines, UUID(line["id"]))
    for url, body in [(base, {"notes": "forbidden"}), (line_path, {"quantity": 3})]:
        assert client.patch(url, headers=auth, json=body).status_code == 409
    assert client.post(f"{base}/issue", headers=auth).status_code == 409
    assert (
        client.post(
            f"{base}/lines",
            headers=auth,
            json={
                "item_id": str(a.item_id),
                "line_number": 2,
                "quantity": 1,
                "unit_price": 0,
            },
        ).status_code
        == 409
    )
    migrator_connection.execute(items.update().where(items.c.id == a.item_id).values(uom="M"))
    migrator_connection.commit()
    successor = client.post(
        f"{line_path}/supersede",
        headers=auth,
        json={
            "item_id": str(a.item_id),
            "quantity": "2.5",
            "unit_price": "2.123456789",
        },
    )
    assert successor.status_code == 201, successor.text
    successor = successor.json()
    assert successor["supersedes_line_id"] == line["id"] and successor["uom"] == "M"
    assert successor["created_by_actor_id"] == str(a.actor_id)
    assert snapshot(migrator_connection, lines, UUID(line["id"])) == original
    history = client.get(f"{base}/lines", headers=auth).json()
    assert [r["id"] for r in history] == [line["id"], successor["id"]]
    assert [r["active"] for r in history] == [False, True]
    assert history[0]["uom"] == "EA"
    assert (
        client.post(
            f"{line_path}/supersede",
            headers=auth,
            json={
                "item_id": str(a.item_id),
                "quantity": 1,
                "unit_price": 1,
            },
        ).status_code
        == 409
    )
    assert client.get(base, headers=auth).json() == issue.json()
    assert any(r["id"] == po["id"] for r in client.get("/purchase-orders", headers=auth).json())


AUTHORITY = [
    "id",
    "org_id",
    "actor_id",
    "created_by_actor_id",
    "updated_by_actor_id",
    "created_at",
    "updated_at",
    "issued_at",
    "issued_by_actor_id",
    "status",
    "active",
    "superseded",
    "superseded_by_id",
    "supersedes_line_id",
    "uom",
    "received_quantity",
    "receipt_id",
    "invoice_id",
    "currency",
    "version",
    "external_id",
    "erp_id",
]


@pytest.mark.parametrize("field", AUTHORITY)
@pytest.mark.parametrize(
    "operation", ["create_po", "edit_po", "issue", "create_line", "edit_line", "supersede"]
)
def test_http_rejects_caller_authority_and_future_workflow_fields(
    field,
    operation,
    draft,
    space_data,
    space_client,
):
    a, (po, line) = space_data[0], draft
    base = f"/purchase-orders/{po['id']}"
    line_input = {"item_id": str(a.item_id), "quantity": 1, "unit_price": 1}
    method, path, body = {
        "create_po": (
            "POST",
            "/purchase-orders",
            {"vendor_party_id": str(a.vendor_id), "po_number": "PO"},
        ),
        "edit_po": ("PATCH", base, {"notes": "edit"}),
        "issue": ("POST", f"{base}/issue", {}),
        "create_line": ("POST", f"{base}/lines", line_input | {"line_number": 2}),
        "edit_line": ("PATCH", f"{base}/lines/{line['id']}", {"quantity": 2}),
        "supersede": ("POST", f"{base}/lines/{line['id']}/supersede", line_input),
    }[operation]
    response = space_client.request(method, path, headers=headers(a), json=body | {field: "spoof"})
    assert response.status_code == 422, response.text
    assert any(e["type"] == "extra_forbidden" for e in response.json()["detail"])


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", 0),
        ("quantity", "-1"),
        ("quantity", "NaN"),
        ("quantity", "Infinity"),
        ("unit_price", "-0.1"),
        ("unit_price", "NaN"),
        ("expected_date", "not-a-date"),
        ("line_number", 0),
        ("line_number", 1.5),
        ("line_number", True),
        ("line_number", 2147483648),
    ],
)
def test_http_invalid_business_values_fail_without_partial_rows(
    field,
    value,
    draft,
    space_data,
    space_client,
    migrator_connection,
):
    a = space_data[0]
    before = migrator_connection.execute(select(lines)).all()
    migrator_connection.rollback()
    result = space_client.post(
        f"/purchase-orders/{draft[0]['id']}/lines",
        headers=headers(a),
        json={"item_id": str(a.item_id), "line_number": 2, "quantity": 1, "unit_price": 0}
        | {field: value},
    )
    assert result.status_code == 422
    assert migrator_connection.execute(select(lines)).all() == before
    migrator_connection.rollback()


@pytest.mark.parametrize(
    "operation", ["read", "list", "edit", "issue", "history", "new", "edit_line", "supersede"]
)
def test_all_procurement_routes_require_credentials(operation, draft, space_client):
    po, line = draft
    base = f"/purchase-orders/{po['id']}"
    method, path = {
        "read": ("GET", base),
        "list": ("GET", "/purchase-orders"),
        "edit": ("PATCH", base),
        "issue": ("POST", f"{base}/issue"),
        "history": ("GET", f"{base}/lines"),
        "new": ("POST", "/purchase-orders"),
        "edit_line": ("PATCH", f"{base}/lines/{line['id']}"),
        "supersede": ("POST", f"{base}/lines/{line['id']}/supersede"),
    }[operation]
    assert space_client.request(method, path, json={}).status_code == 401


def test_rls_fail_closed_cross_tenant_and_hidden_api_targets(
    draft,
    space_data,
    space_client,
    app_connection,
):
    a, b = space_data
    po, line = draft
    with app_connection.begin():
        assert app_connection.execute(select(orders)).all() == []
        assert app_connection.execute(select(lines)).all() == []
    with app_connection.begin():
        set_authenticated(app_connection, b)
        assert app_connection.execute(select(orders)).all() == []
        assert app_connection.execute(select(lines)).all() == []
        assert (
            app_connection.execute(
                orders.update().where(orders.c.id == po["id"]).values(notes="hidden")
            ).rowcount
            == 0
        )
        assert (
            app_connection.execute(
                lines.update().where(lines.c.id == line["id"]).values(quantity=4)
            ).rowcount
            == 0
        )
    # WITH CHECK is exercised on an otherwise valid foreign-tenant insert.
    with pytest.raises(DBAPIError) as error:
        write(app_connection, a, orders.insert().values(order_values(b)).returning(orders))
    assert error.value.orig.sqlstate == "42501"
    for identity in (po["id"], uuid7()):
        base = f"/purchase-orders/{identity}"
        for method, path, body in [
            ("GET", base, None),
            ("GET", f"{base}/lines", None),
            ("PATCH", base, {"notes": "hidden"}),
            ("POST", f"{base}/issue", {}),
        ]:
            assert (
                space_client.request(method, path, headers=headers(b), json=body).status_code == 404
            )


@pytest.mark.parametrize("table", [orders, lines], ids=lambda t: t.name)
@pytest.mark.parametrize("privilege", ["DELETE", "TRUNCATE", "TRIGGER", "REFERENCES"])
def test_runtime_has_no_destructive_or_schema_privileges(table, privilege, app_connection):
    assert (
        app_connection.execute(
            text("SELECT has_table_privilege(current_user,:table,:priv)"),
            {"table": f"fleetops.{table.name}", "priv": privilege},
        ).scalar()
        is False
    )


@pytest.mark.parametrize("table", [orders, lines], ids=lambda t: t.name)
@pytest.mark.parametrize("attack", ["creator", "updater", "organization_only", "actor_guc"])
def test_direct_sql_creation_cannot_spoof_performer(
    table,
    attack,
    draft,
    space_data,
    other_human,
    app_connection,
):
    a = space_data[0]
    values = order_values(a) if table is orders else line_values(a, draft[0]["id"], line_number=2)
    if attack in {"creator", "actor_guc"}:
        values["created_by_actor_id"] = other_human.actor_id
    if attack == "updater":
        values["updated_by_actor_id"] = other_human.actor_id
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if attack == "organization_only":
                set_organization(app_connection, a.org_id)
            else:
                set_authenticated(app_connection, a)
            app_connection.execute(
                text("SELECT set_config('fleetops.actor_id', :actor, true)"),
                {"actor": str(other_human.actor_id)},
            )
            app_connection.execute(table.insert().values(values))
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("table", [orders, lines], ids=lambda t: t.name)
def test_direct_draft_update_binds_real_updater_and_time(
    table,
    draft,
    space_data,
    other_human,
    app_connection,
):
    row = draft[0 if table is orders else 1]
    values = {"notes": "changed"} if table is orders else {"quantity": "0.25"}
    changed = write(
        app_connection,
        other_human,
        table.update().where(table.c.id == row["id"]).values(values).returning(table),
    )
    assert changed["created_at"] == row["created_at"]
    assert changed["created_by_actor_id"] == space_data[0].actor_id
    assert changed["updated_by_actor_id"] == other_human.actor_id
    assert changed["updated_at"] > row["updated_at"]
    # The accepted updater trigger replaces SQL testimony rather than rejecting it.
    spoofed = write(
        app_connection,
        space_data[0],
        table.update()
        .where(table.c.id == row["id"])
        .values(updated_by_actor_id=other_human.actor_id, updated_at="1900-01-01T00:00:00Z")
        .returning(table),
    )
    assert spoofed["updated_by_actor_id"] == space_data[0].actor_id
    assert spoofed["updated_at"] > changed["updated_at"]


def test_procurement_has_no_asset_or_prior_source_of_truth_effect(
    asset_data,
    space_client,
    migrator_connection,
):
    before = schema_snapshot(migrator_connection)
    a = asset_data[0]
    auth = headers(a)
    po = space_client.post(
        "/purchase-orders",
        headers=auth,
        json={"vendor_party_id": str(a.vendor_id), "po_number": "CONTEXT"},
    ).json()
    path = f"/purchase-orders/{po['id']}"
    assert space_client.patch(path, headers=auth, json={"notes": "Expectation"}).status_code == 200
    payload = {"item_id": str(a.item_id), "quantity": 1, "unit_price": 5}
    line = space_client.post(
        f"{path}/lines", headers=auth, json=payload | {"line_number": 1}
    ).json()
    assert (
        space_client.patch(
            f"{path}/lines/{line['id']}", headers=auth, json={"quantity": 2}
        ).status_code
        == 200
    )
    assert space_client.post(f"{path}/issue", headers=auth).status_code == 200
    assert (
        space_client.post(
            f"{path}/lines/{line['id']}/supersede", headers=auth, json=payload | {"quantity": 3}
        ).status_code
        == 201
    )
    after = schema_snapshot(migrator_connection)
    assert {
        k: v for k, v in after.items() if k not in {"purchase_orders", "purchase_order_lines"}
    } == {k: v for k, v in before.items() if k not in {"purchase_orders", "purchase_order_lines"}}
    assert space_client.post("/assets", headers=auth, json={}).status_code == 405


def test_po_number_is_display_context_and_vendor_is_not_manufacturer(space_data, app_connection):
    a = space_data[0]
    first = write(app_connection, a, orders.insert().values(order_values(a)).returning(orders))
    second = write(app_connection, a, orders.insert().values(order_values(a)).returning(orders))
    assert first["id"] != second["id"] and first["po_number"] == second["po_number"]
    assert first["vendor_party_id"] == a.vendor_id != a.party_id
