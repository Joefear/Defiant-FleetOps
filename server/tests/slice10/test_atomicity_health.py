"""Fault injection proves whole correction rollback and read-only corruption detection."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    ASSET_TABLES,
    RECEIVING_TABLES,
    WHEN,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_movements, metadata, receipt_evaluation_lines
from fleetops.domain import asset_facts, corrections, record_corrections
from fleetops.domain.assets import AssetConflict

CONSEQUENCE_TABLES = tuple(
    metadata.tables["fleetops." + name]
    for name in (
        "receipt_line_corrections",
        "receipt_correction_evaluations",
        "receipt_evaluation_lines",
        "receipt_evaluation_expectations",
        "exception_workflows",
        "exception_events",
        "receipt_evaluation_exceptions",
    )
)


@pytest.mark.parametrize(
    "stage",
    [
        "receipt_line_corrections",
        "receipt_correction_evaluations",
        "receipt_evaluation_lines",
        "receipt_evaluation_exceptions",
        "receiving_exceptions",
        "exception_events",
        "exception_workflows",
    ],
)
def test_failed_required_consequence_rolls_back_every_domain_row(
    stage,
    space_data,
    make_item,
    make_order,
    receiving_client,
    migrator_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], unit=None, condition="DAMAGED"
                )
            ],
        ),
    )
    tables = RECEIVING_TABLES + ASSET_TABLES + CONSEQUENCE_TABLES
    before = snapshot(migrator_connection, tenant.org_id, tables)
    name = "slice10_fault_" + uuid7().hex
    operation = "UPDATE" if stage == "exception_workflows" else "INSERT"
    condition = (
        "WHEN (NEW.correction_role='CORRECTED')" if stage == "receipt_line_corrections" else ""
    )
    migrator_connection.execute(
        text(f"""
      CREATE FUNCTION fleetops.{name}() RETURNS trigger LANGUAGE plpgsql
      SET search_path=pg_catalog,pg_temp AS $fault$
      BEGIN RAISE EXCEPTION USING ERRCODE='P0001',MESSAGE='Controlled consequence fault';
      END $fault$;
      REVOKE ALL ON FUNCTION fleetops.{name}() FROM PUBLIC,fleetops_app,fleetops_authenticator;
      CREATE TRIGGER {name} BEFORE {operation} ON fleetops.{stage}
        FOR EACH ROW {condition} EXECUTE FUNCTION fleetops.{name}();
    """)
    )
    migrator_connection.commit()
    try:
        response = receiving_client.post(
            f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
            headers=headers(tenant),
            json=json_data(
                dict(
                    expected_generation=0,
                    condition="GOOD",
                    quantity="2",
                    reason="Repair both observed facts",
                    correction_occurred_at=WHEN,
                )
            ),
        )
        assert response.status_code == 409, response.text
        assert snapshot(migrator_connection, tenant.org_id, tables) == before
    finally:
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(f"DROP TRIGGER {name} ON fleetops.{stage}")
        migrator_connection.exec_driver_sql(f"DROP FUNCTION fleetops.{name}()")
        migrator_connection.commit()


@pytest.mark.parametrize("kind", ["procurement", "receipt"])
def test_record_half_pair_cannot_commit(
    kind, space_data, make_item, make_order, receiving_client, app_connection
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    if kind == "receipt":
        captured = post_receipt(
            receiving_client,
            tenant,
            receipt_data(
                tenant,
                po_id=po["id"],
                lines=[line_data(tenant, item_id=item, po_line_id=lines[0]["id"], unit=None)],
            ),
        )
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            original = dict(
                app_connection.execute(
                    select(metadata.tables["fleetops.receipt_lines"]).where(
                        metadata.tables["fleetops.receipt_lines"].c.id == captured["lines"][0]["id"]
                    )
                )
                .mappings()
                .one()
            )
        table = metadata.tables["fleetops.receipt_line_corrections"]
        fields = record_corrections.LINE_FIELDS
        root, entity = "receipt_line_id", "receipt_id"
    else:
        original = lines[0]
        table = metadata.tables["fleetops.purchase_order_line_corrections"]
        fields = record_corrections.PO_FIELDS
        root, entity = "po_line_id", "po_id"
    with pytest.raises(DBAPIError) as denied:
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            app_connection.execute(
                table.insert().values(
                    **{field: original[field] for field in fields},
                    **{root: original["id"], entity: original[entity]},
                    id=uuid7(),
                    org_id=tenant.org_id,
                    correction_role="REVERSAL",
                    correction_pair_id=uuid7(),
                    correction_generation=1,
                    correction_occurred_at=WHEN,
                    occurred_at=WHEN,
                    reason="Half pair",
                )
            )
    assert denied.value.orig.sqlstate == "23514"
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        assert app_connection.execute(select(table.c.id)).all() == []


def test_privileged_half_pair_is_reported_and_blocks_admission(
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
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.asset_movements DISABLE TRIGGER correction_pair_complete"
    )
    migrator_connection.execute(
        asset_movements.insert().values(
            id=uuid7(),
            org_id=tenant.org_id,
            asset_id=tenant.asset_id,
            result_version=3,
            from_location_id=tenant.location_id,
            to_location_id=tenant.other_location_id,
            actor_id=tenant.actor_id,
            reason="Damaged pair",
            occurred_at=WHEN,
            corrects_movement_id=root["id"],
            correction_role="REVERSAL",
            correction_pair_id=uuid7(),
            correction_generation=1,
            correction_occurred_at=WHEN,
        )
    )
    migrator_connection.commit()
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.asset_movements ENABLE TRIGGER correction_pair_complete"
    )
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, tenant)
        issues = asset_facts.reconcile_assets(app_connection)[0]["discrepancies"]
        assert "correction_history_malformed" in issues
        assert "global_history_version_ahead" in issues
    with pytest.raises(AssetConflict):
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            corrections.correct_movement(
                app_connection,
                tenant.asset_id,
                root["id"],
                values=dict(
                    expected_version=2,
                    to_location_id=None,
                    reason="Must not heal damaged history",
                    correction_occurred_at=WHEN,
                ),
            )


def test_missing_evaluation_source_is_reported_without_repair(
    space_data,
    make_item,
    make_order,
    receiving_client,
    migrator_connection,
):
    tenant = space_data[0]
    item = make_item()
    po, lines = make_order(specs=[dict(item_id=item, quantity=1)])
    captured = post_receipt(
        receiving_client,
        tenant,
        receipt_data(
            tenant,
            po_id=po["id"],
            lines=[
                line_data(
                    tenant, item_id=item, po_line_id=lines[0]["id"], unit=None, condition="DAMAGED"
                )
            ],
        ),
    )
    url = f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections"
    response = receiving_client.post(
        url,
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=0,
                condition="GOOD",
                reason="Inspection correction",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert response.status_code == 201, response.text
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.receipt_evaluation_lines DISABLE TRIGGER evaluation_guard"
    )
    migrator_connection.execute(
        receipt_evaluation_lines.delete().where(receipt_evaluation_lines.c.org_id == tenant.org_id)
    )
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.receipt_evaluation_lines ENABLE TRIGGER evaluation_guard"
    )
    migrator_connection.commit()
    before = snapshot(migrator_connection, tenant.org_id, RECEIVING_TABLES + CONSEQUENCE_TABLES)
    result = receiving_client.get("/health/corrections", headers=headers(tenant))
    assert result.status_code == 200, result.text
    assert "evaluation_line_population" in {issue["issue"] for issue in result.json()}
    rejected = receiving_client.post(
        url,
        headers=headers(tenant),
        json=json_data(
            dict(
                expected_generation=1,
                quantity="2",
                reason="Must not repair missing authority",
                correction_occurred_at=WHEN,
            )
        ),
    )
    assert rejected.status_code == 409, rejected.text
    assert (
        snapshot(migrator_connection, tenant.org_id, RECEIVING_TABLES + CONSEQUENCE_TABLES)
        == before
    )
