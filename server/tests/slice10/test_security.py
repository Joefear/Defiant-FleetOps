"""Real runtime privileges, credential admission and strict bounded correction requests."""

from contextlib import contextmanager

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import WHEN, headers, json_data
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_movements, metadata
from fleetops.domain import asset_facts, corrections
from fleetops.domain.assets import AssetInvalid, AssetNotFound

NEW_TABLES = (
    "purchase_order_line_corrections",
    "receipt_line_corrections",
    "receipt_correction_evaluations",
    "receipt_evaluation_lines",
    "receipt_evaluation_expectations",
    "exception_workflows",
    "exception_events",
    "receipt_evaluation_exceptions",
)
AUTHORITY_FIELDS = (
    "org_id",
    "actor_id",
    "recorded_at",
    "correction_role",
    "correction_pair_id",
    "correction_generation",
    "result_version",
    "source_id",
    "evaluation_id",
    "client_op_id",
    "evidence_ref",
    "created_by_actor_id",
    "updated_by_actor_id",
    "recording_transaction_id",
)


@pytest.mark.parametrize(
    "path,fields",
    [
        ("transitions", {"to_state": "IN_STOCK"}),
        ("movements", {"to_location_id": None}),
        ("ownership-changes", {"to_owner_party_id": str(uuid7())}),
        ("custody-changes", {"to_custodian_party_id": None}),
        ("assignments", {"to_assignee_type": None, "to_assignee_id": None}),
    ],
)
@pytest.mark.parametrize("forbidden", AUTHORITY_FIELDS)
def test_asset_api_rejects_caller_authority(path, fields, forbidden, asset_data, receiving_client):
    tenant = asset_data[0]
    body = dict(
        fields, expected_version=1, reason="Correction request", correction_occurred_at=WHEN
    )
    body[forbidden] = "caller authority"
    response = receiving_client.post(
        f"/assets/{tenant.asset_id}/{path}/{uuid7()}/corrections",
        headers=headers(tenant),
        json=json_data(body),
    )
    assert response.status_code == 422, response.text
    assert any(
        error["type"] == "extra_forbidden" and error["loc"][-1] == forbidden
        for error in response.json()["detail"]
    )


@pytest.mark.parametrize("kind", ["receipt", "procurement"])
@pytest.mark.parametrize(
    "forbidden", AUTHORITY_FIELDS + ("supersedes_line_id", "asset_id", "serialized")
)
def test_record_api_rejects_authority_and_creation_fields(
    kind, forbidden, space_data, receiving_client
):
    tenant = space_data[0]
    path = (
        f"/receipts/{uuid7()}/lines/{uuid7()}/corrections"
        if kind == "receipt"
        else f"/purchase-orders/{uuid7()}/lines/{uuid7()}/corrections"
    )
    response = receiving_client.post(
        path,
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                reason="Correction request",
                correction_occurred_at=WHEN,
                **{forbidden: "caller authority"},
            )
        ),
    )
    assert response.status_code == 422, response.text
    assert any(error["type"] == "extra_forbidden" for error in response.json()["detail"])


@pytest.mark.parametrize("reason", [None, "", " ", "\t\r\n", "x" * 4001])
def test_reason_is_required_normalized_and_bounded(reason, asset_data, receiving_client):
    tenant = asset_data[0]
    response = receiving_client.post(
        f"/assets/{tenant.asset_id}/movements/{uuid7()}/corrections",
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_version=1, to_location_id=None, reason=reason, correction_occurred_at=WHEN
            )
        ),
    )
    assert response.status_code == 422


@pytest.mark.parametrize("name", NEW_TABLES)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_new_history_and_status_have_no_ordinary_mutation_authority(
    name,
    operation,
    space_data,
    app_connection,
):
    tenant = space_data[0]
    column = "status" if name == "exception_workflows" else "org_id"
    statement = (
        f"UPDATE fleetops.{name} SET {column}={column}"
        if operation == "UPDATE"
        else f"{operation} {'FROM ' if operation == 'DELETE' else ''}fleetops.{name}"
    )
    with pytest.raises(DBAPIError) as denied:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            app_connection.exec_driver_sql(statement)
    assert denied.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("name", NEW_TABLES)
def test_new_tables_have_both_tenant_policies_and_safe_privileges(name, migrator_connection):
    rows = (
        migrator_connection.execute(
            text("""
      SELECT permissive,roles,cmd,qual,with_check FROM pg_policies
      WHERE schemaname='fleetops' AND tablename=:name ORDER BY policyname
    """),
            {"name": name},
        )
        .mappings()
        .all()
    )
    assert [row["permissive"] for row in rows] == ["PERMISSIVE", "RESTRICTIVE"]
    assert all(
        row["roles"] == ["fleetops_app"]
        and row["qual"] == row["with_check"]
        and "org_id" in row["qual"]
        and "NULLIF" in row["qual"]
        for row in rows
    )
    for column in (
        "actor_id",
        "recorded_at",
        "source_role",
        "conflicting_asset_id",
        "recording_transaction_id",
    ):
        if column not in metadata.tables["fleetops." + name].c:
            continue
        assert not migrator_connection.execute(
            text("SELECT has_column_privilege(:role,:table,:column,'INSERT')"),
            dict(role="fleetops_app", table="fleetops." + name, column=column),
        ).scalar_one()


@pytest.mark.parametrize(
    "context", ["missing", "foreign_asset", "foreign_root", "actor_override", "invalid_credential"]
)
def test_correction_runtime_credential_and_tenant_boundary(context, asset_data, app_connection):
    tenant, other = asset_data
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = asset_facts.move_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                to_location_id=tenant.other_location_id,
                reason="Original",
                occurred_at=WHEN,
            ),
        )
    with pytest.raises((AssetInvalid, AssetNotFound)):
        with app_connection.begin():
            if context != "missing":
                set_authenticated(app_connection, tenant)
            if context == "actor_override":
                app_connection.execute(
                    text("SELECT set_config('fleetops.actor_id',:actor,true)"),
                    {"actor": str(tenant.actor_id)},
                )
            if context == "invalid_credential":
                app_connection.execute(
                    text("SELECT set_config('fleetops.session_digest_hex',:digest,true)"),
                    {"digest": "00" * 32},
                )
            corrections.correct_movement(
                app_connection,
                other.asset_id if context == "foreign_asset" else tenant.asset_id,
                uuid7() if context == "foreign_root" else root["id"],
                values=dict(
                    expected_version=2,
                    to_location_id=None,
                    reason="Attempt",
                    correction_occurred_at=WHEN,
                ),
            )
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert len(asset_facts.list_movements(app_connection, tenant.asset_id)) == 1


@contextmanager
def isolated_history_insert(migrator_connection, app_connection):
    """Temporarily grant INSERT solely to isolate deferred invariants from the ACL gate."""
    migrator_connection.exec_driver_sql("GRANT INSERT ON fleetops.asset_movements TO fleetops_app")
    migrator_connection.commit()
    try:
        yield
    finally:
        app_connection.rollback()
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "REVOKE INSERT ON fleetops.asset_movements FROM fleetops_app"
        )
        migrator_connection.commit()


def test_half_pair_cannot_commit_even_with_isolated_insert_grant(
    asset_data, app_connection, migrator_connection
):
    tenant = asset_data[0]
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        root = asset_facts.move_asset(
            app_connection,
            tenant.asset_id,
            values=dict(
                expected_version=1,
                to_location_id=tenant.other_location_id,
                reason="Original",
                occurred_at=WHEN,
            ),
        )
    with isolated_history_insert(migrator_connection, app_connection):
        with pytest.raises(DBAPIError) as denied:
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                app_connection.execute(
                    asset_movements.insert().values(
                        id=uuid7(),
                        org_id=tenant.org_id,
                        asset_id=tenant.asset_id,
                        result_version=3,
                        from_location_id=tenant.location_id,
                        to_location_id=tenant.other_location_id,
                        actor_id=tenant.actor_id,
                        reason="Half pair",
                        occurred_at=WHEN,
                        corrects_movement_id=root["id"],
                        correction_role="REVERSAL",
                        correction_pair_id=uuid7(),
                        correction_generation=1,
                        correction_occurred_at=WHEN,
                    )
                )
        assert denied.value.orig.sqlstate == "23514"
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert app_connection.execute(
            select(asset_movements.c.id).where(asset_movements.c.asset_id == tenant.asset_id)
        ).scalars().all() == [root["id"]]


def test_no_generic_or_deferred_correction_routes(receiving_client):
    paths = receiving_client.get("/openapi.json").json()["paths"]
    assert "/corrections" not in paths
    assert "/exceptions" not in paths or "post" not in paths["/exceptions"]
    for path in paths:
        if "corrections" in path:
            assert not any(
                forbidden in path
                for forbidden in (
                    "identifiers",
                    "initial-facts",
                    "initial-assignment",
                    "configurations",
                    "evidence",
                    "materials",
                    "offline",
                )
            )
            assert "/lines/" in path or path.startswith("/assets/") or path == "/health/corrections"
