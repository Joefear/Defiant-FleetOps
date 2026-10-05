"""Tenant isolation, correction generations, provenance and snapshot boundaries on PostgreSQL."""

from datetime import datetime
from uuid import uuid4

import pytest
from server.tests.slice15.helpers import (
    EARLY,
    PRICE,
    WHEN,
    attach,
    call,
    capture_line,
    entries,
    headers,
    move,
    receive,
)
from sqlalchemy import event, select

from fleetops.db.metadata import asset_movements, assets


def test_history_preserves_two_corrections_and_clock_claims(scenario_client, space_data):
    client, tenant = scenario_client, space_data[0]
    received = receive(client, tenant, [capture_line(tenant, tenant.item_id)])
    asset = received["lines"][0]["asset_id"]
    root = move(client, tenant, asset, tenant.other_location_id)
    first = call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/movements/{root['id']}/corrections",
        {
            "expected_version": 2,
            "to_location_id": None,
            "reason": "Actual destination unknown",
            "correction_occurred_at": WHEN,
            "occurred_at": EARLY,
        },
        status=201,
    )
    second = call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/movements/{root['id']}/corrections",
        {
            "expected_version": 4,
            "to_location_id": str(tenant.location_id),
            "reason": "Verified destination",
            "correction_occurred_at": WHEN,
            "occurred_at": EARLY,
        },
        status=201,
    )
    history = call(client, tenant, "GET", f"/assets/{asset}/history")
    moves = entries(history, "MOVEMENT")
    assert len(moves) == 5
    reversal = {
        e["facts"]["correction_generation"]: e
        for e in moves
        if e["facts"]["correction_role"] == "REVERSAL"
    }
    assert reversal[1]["cancels_history_id"] == root["id"]
    assert reversal[2]["cancels_history_id"] == first["id"]
    assert next(e for e in moves if e["id"] == second["id"])["result_version"] == 6
    assert history["asset"]["version"] == 6
    assert history["asset"]["current_location_id"] == str(tenant.location_id)
    assert sorted(e["result_version"] for e in history["entries"] if e["result_version"]) == list(
        range(1, 7)
    )
    assert datetime.fromisoformat(moves[0]["occurred_at"]).year == 1970
    assert call(client, tenant, "GET", "/health/reconciliation")["discrepancy_count"] == 0


def test_receipt_and_procurement_corrections_keep_selected_price_and_all_evidence(
    scenario_client,
    space_data,
    make_order,
):
    client, tenant = scenario_client, space_data[0]
    order, lines = make_order(specs=[{"item_id": tenant.item_id, "quantity": 1}])
    correction = call(
        client,
        tenant,
        "POST",
        f"/purchase-orders/{order['id']}/lines/{lines[0]['id']}/corrections",
        {
            "expected_generation": 0,
            "unit_price": PRICE,
            "reason": "Correct original price",
            "correction_occurred_at": WHEN,
        },
        status=201,
    )
    receipt = receive(
        client,
        tenant,
        [capture_line(tenant, tenant.item_id, str(lines[0]["id"])) | {"expected_po_generation": 1}],
        order=str(order["id"]),
        comparators=[],
    )
    asset = receipt["lines"][0]["asset_id"]
    line = receipt["lines"][0]
    call(
        client,
        tenant,
        "POST",
        f"/receipts/{receipt['id']}/lines/{line['id']}/corrections",
        {
            "expected_generation": 0,
            "condition": "DAMAGED",
            "expected_po_generation": 1,
            "reason": "Observed damage was omitted",
            "correction_occurred_at": WHEN,
        },
        status=201,
    )
    exception = call(client, tenant, "GET", "/exceptions/open?asset_id=" + asset)[0]
    configuration = call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/configurations",
        {
            "image_name": "base",
            "image_version": "1",
            "config_profile": "floor",
            "applied_at": EARLY,
        },
        status=201,
    )
    targets = [
        ("ASSET", asset, "OTHER"),
        ("RECEIPT", receipt["id"], "RECEIVING_EVIDENCE"),
        ("RECEIPT_LINE", line["id"], "RECEIVING_EVIDENCE"),
        ("ASSET_CONFIGURATION", configuration["id"], "CONFIG_EVIDENCE"),
        ("RECEIVING_EXCEPTION", exception["id"], "EXCEPTION_EVIDENCE"),
    ]
    linked = [
        attach(client, tenant, kind, identifier, role, content=kind.encode())
        for kind, identifier, role in targets
    ]
    history = call(client, tenant, "GET", f"/assets/{asset}/history")
    assert len(entries(history, "PROCUREMENT_CORRECTION")) == 2
    assert len(entries(history, "RECEIPT_CORRECTION")) == 2
    comparator = entries(history, "RECEIPT_COMPARATOR")[0]["facts"]
    assert comparator["source_generation"] == 1 and comparator["source_id"] == correction["id"]
    assert comparator["expected_line"]["unit_price"] == PRICE
    assert {entry["id"] for entry in entries(history, "ATTACHMENT_LINK")} == {
        link[1]["id"] for link in linked
    }
    assert {entry["id"] for entry in entries(history, "ATTACHMENT")} == {
        link[0]["id"] for link in linked
    }
    assert all(
        "configuration_seq" not in entry["facts"]
        and "storage_key" not in entry["facts"]
        and "recording_transaction_id" not in entry["facts"]
        for entry in history["entries"]
    )
    assert all(
        entry["occurred_at_source"] == "recorded_link"
        for entry in entries(history, "ATTACHMENT_LINK")
    )
    assert history["asset"]["version"] == 1
    assert call(client, tenant, "GET", "/health/reconciliation")["discrepancy_count"] == 0


def test_reads_fail_closed_and_preserve_absence(scenario_client, space_data, seed_asset):
    client, tenant, foreign = scenario_client, *space_data
    visible = seed_asset(tenant)["id"]
    foreign_asset = seed_asset(foreign)["id"]
    history = call(client, tenant, "GET", f"/assets/{visible}/history")
    assert entries(history, "RECEIPT") == [] and entries(history, "RECEIPT_LINE") == []
    for asset in (foreign_asset, uuid4()):
        response = client.get(f"/assets/{asset}/history", headers=headers(tenant))
        assert response.status_code == 404 and response.json() == {"detail": "Asset not found"}
    for path in (f"/assets/{visible}/history", "/health/reconciliation"):
        for authorization in ({}, {"Authorization": "Bearer invalid"}):
            assert client.get(path, headers=authorization).status_code == 401
    assert call(client, tenant, "GET", f"/assets/{visible}/history")["asset"]["id"] == str(visible)


@pytest.mark.parametrize("corruption", ["gap", "duplicate"])
def test_aggregate_preserves_global_checks_and_tenant_scope(
    corruption,
    scenario_client,
    space_data,
    seed_asset,
    migrator_connection,
):
    client, tenant, foreign = scenario_client, *space_data
    asset = seed_asset(tenant)["id"]
    other = seed_asset(foreign)["id"]
    move(client, tenant, asset, tenant.other_location_id)
    assert call(client, tenant, "GET", "/health/reconciliation")["discrepancy_count"] == 0
    if corruption == "gap":
        migrator_connection.execute(assets.update().where(assets.c.id == asset).values(version=3))
        expected = "global_version_missing"
    else:
        original = dict(
            migrator_connection.execute(
                select(asset_movements).where(asset_movements.c.asset_id == asset)
            )
            .mappings()
            .one()
        )
        original.update(id=uuid4(), result_version=1)
        migrator_connection.execute(asset_movements.insert().values(**original))
        expected = "global_version_duplicate"
    migrator_connection.execute(assets.update().where(assets.c.id == other).values(version=2))
    migrator_connection.commit()
    health = call(client, tenant, "GET", "/health/reconciliation")
    assert health["discrepancy_count"] == health["affected_asset_count"] == 1
    assert len(health["assets"]) == 1 and health["assets"][0]["asset_id"] == str(asset)
    assert expected in health["assets"][0]["discrepancies"]
    assert call(client, foreign, "GET", "/health/reconciliation")["assets"][0]["asset_id"] == str(
        other
    )


def test_history_read_retains_one_snapshot_when_writer_commits(
    scenario_client,
    space_data,
    seed_asset,
):
    client, tenant = scenario_client, space_data[0]
    asset = seed_asset(tenant)["id"]
    observed = []

    def commit_after_snapshot(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("\nWITH unit AS"):
            observed.append("history snapshot acquired")
            move(client, tenant, asset, tenant.other_location_id)
            call(
                client,
                tenant,
                "POST",
                f"/assets/{asset}/configurations",
                {
                    "image_name": "snapshot",
                    "image_version": "1",
                    "config_profile": "floor",
                    "applied_at": EARLY,
                },
                status=201,
            )

    event.listen(client.app.state.engine, "after_cursor_execute", commit_after_snapshot)
    try:
        history = call(client, tenant, "GET", f"/assets/{asset}/history")
    finally:
        event.remove(client.app.state.engine, "after_cursor_execute", commit_after_snapshot)
    assert observed == ["history snapshot acquired"]
    assert history["asset"]["version"] == 1 and entries(history, "MOVEMENT") == []
    assert history["configuration_order"] == [] and entries(history, "CONFIGURATION") == []
    after = call(client, tenant, "GET", f"/assets/{asset}/history")
    assert after["asset"]["version"] == 2 and len(entries(after, "MOVEMENT")) == 1
    assert after["configuration_order"] == [
        entry["id"] for entry in entries(after, "CONFIGURATION")
    ]
    assert len(after["configuration_order"]) == 1


def test_health_read_retains_one_snapshot_when_owner_corrupts(
    scenario_client,
    space_data,
    seed_asset,
    migrator_connection,
):
    client, tenant = scenario_client, space_data[0]
    asset = seed_asset(tenant)["id"]
    observed = []

    def corrupt_after_snapshot(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("\nSELECT\n  COALESCE"):
            observed.append("aggregate snapshot acquired")
            migrator_connection.execute(
                assets.update().where(assets.c.id == asset).values(version=2)
            )
            migrator_connection.commit()

    event.listen(client.app.state.engine, "after_cursor_execute", corrupt_after_snapshot)
    try:
        health = call(client, tenant, "GET", "/health/reconciliation")
    finally:
        event.remove(client.app.state.engine, "after_cursor_execute", corrupt_after_snapshot)
    assert observed == ["aggregate snapshot acquired"] and health["discrepancy_count"] == 0
    assert call(client, tenant, "GET", "/health/reconciliation")["discrepancy_count"] == 1


def test_serial_collision_correction_remains_context_not_second_receipt(
    scenario_client, space_data
):
    client, tenant = scenario_client, space_data[0]
    first_line, third_line = [capture_line(tenant, tenant.item_id) for _ in range(2)]
    first = receive(client, tenant, [first_line])
    third = receive(client, tenant, [third_line])
    second_line = capture_line(tenant, tenant.item_id)
    second_line["unit"]["identifier"] = dict(first_line["unit"]["identifier"])
    conflicting = receive(client, tenant, [second_line])
    assert conflicting["lines"][0]["asset_id"] is None
    corrected = call(
        client,
        tenant,
        "POST",
        f"/receipts/{conflicting['id']}/lines/{conflicting['lines'][0]['id']}/corrections",
        {
            "expected_generation": 0,
            "reason": "Observed third unit instead",
            "correction_occurred_at": WHEN,
            "observed_identifier_type": "MANUFACTURER_SERIAL",
            "observed_identifier_value": third_line["unit"]["identifier"]["value"],
        },
        status=201,
    )
    third_id = third["lines"][0]["asset_id"]
    assert corrected["asset_id"] is None and corrected["conflicting_asset_id"] == third_id
    for receipt in (first, third):
        asset = receipt["lines"][0]["asset_id"]
        history = call(client, tenant, "GET", f"/assets/{asset}/history")
        assert history["asset"]["version"] == 1
        assert {e["facts"]["receipt_id"] for e in entries(history, "RECEIPT_LINE")} == {
            receipt["id"],
            conflicting["id"],
        }
        assert len(entries(history, "RECEIPT_CORRECTION")) == 2
        assert len(entries(history, "TRANSITION")) == 1


def test_aggregate_includes_python_corrected_lifecycle_issues(
    scenario_client,
    space_data,
    seed_asset,
    migrator_connection,
):
    from server.tests.slice15.helpers import transition

    from fleetops.db.metadata import asset_transitions

    client, tenant = scenario_client, space_data[0]
    asset = seed_asset(tenant)["id"]
    root = transition(client, tenant, asset, "IN_STOCK")
    corrected = call(
        client,
        tenant,
        "POST",
        f"/assets/{asset}/transitions/{root['id']}/corrections",
        {
            "expected_version": 2,
            "to_state": "ON_HOLD",
            "reason": "Held at receipt",
            "correction_occurred_at": WHEN,
        },
        status=201,
    )
    assert call(client, tenant, "GET", "/health/reconciliation")["discrepancy_count"] == 0
    # Owner-only fault injection cannot become a runtime repair capability.
    migrator_connection.exec_driver_sql(
        "ALTER TABLE fleetops.asset_transitions DISABLE TRIGGER USER"
    )
    try:
        migrator_connection.execute(
            asset_transitions.update()
            .where(asset_transitions.c.id == corrected["id"])
            .values(to_state="RETIRED")
        )
        migrator_connection.commit()
    finally:
        migrator_connection.rollback()
        migrator_connection.exec_driver_sql(
            "ALTER TABLE fleetops.asset_transitions ENABLE TRIGGER USER"
        )
        migrator_connection.commit()
    health = call(client, tenant, "GET", "/health/reconciliation")
    assert health["discrepancy_count"] == 1
    assert "illegal_corrected_lifecycle" in health["assets"][0]["discrepancies"]
    assert "illegal_corrected_lifecycle" in {row["issue"] for row in health["corrections"]}


def test_history_retains_configuration_authority_under_reversed_clocks(
    scenario_client,
    space_data,
    seed_asset,
):
    client, tenant = scenario_client, space_data[0]
    asset = seed_asset(tenant)["id"]
    assert call(client, tenant, "GET", f"/assets/{asset}/history")["configuration_order"] == []
    configurations = []
    for version, occurred in (("old", WHEN), ("new", EARLY)):
        configurations.append(
            call(
                client,
                tenant,
                "POST",
                f"/assets/{asset}/configurations",
                {
                    "image_name": "factory",
                    "image_version": version,
                    "config_profile": "observed",
                    "applied_at": occurred,
                },
                status=201,
            )
        )
    history = call(client, tenant, "GET", f"/assets/{asset}/history")
    ids = [configuration["id"] for configuration in configurations]
    assert [entry["id"] for entry in entries(history, "CONFIGURATION")] == ids[::-1]
    assert call(client, tenant, "GET", f"/assets/{asset}/configurations/current")["id"] == ids[-1]
    assert history.get("configuration_order") == ids, (
        "Combined history retains both configuration records but loses their allocation-order "
        "authority. Occurrence presentation is reversed, and clocks/UUIDs cannot identify "
        "the current configuration under ADR-008 Decisions 20/21."
    )
    assert history["asset"]["version"] == 1
    assert all(
        entry["result_version"] is None and "configuration_seq" not in entry["facts"]
        for entry in entries(history, "CONFIGURATION")
    )
