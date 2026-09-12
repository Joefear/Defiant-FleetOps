"""Durable matrix, coherence, immutability, completeness, and actor boundary proofs."""

from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.test_authentication_boundary import other_human as human_fixture
from server.tests.slice9.conftest import (
    RECEIVING_TABLES,
    UNIT_CALL,
    exception_values,
    headers,
    json_data,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
    unit_parameters,
)
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import (
    receipt_lines,
    receipt_reconciliations,
    receipts,
    receiving_exceptions,
)
from fleetops.db.tenancy import set_credential_context, set_organization

other_human = human_fixture
KINDS = [
    "SHORT",
    "OVER",
    "SUBSTITUTION",
    "DAMAGED",
    "OPENED",
    "SERIAL_UNREADABLE",
    "SERIAL_MISMATCH",
    "UNEXPECTED_ITEM",
    "QUANTITY_VARIANCE",
    "UOM_MISMATCH",
]


@pytest.fixture
def matrix_receipt(receiving_client, asset_data, make_item, make_order):
    """Ten independently known disagreements give valid exemplars of every matrix row."""
    a = asset_data[0]
    plain, substitute = make_item(), make_item()
    po, expected = make_order(
        specs=[
            {"item_id": plain, "quantity": 2},
            {"item_id": plain, "quantity": 1},
            {"item_id": plain, "quantity": 1},
            {"item_id": a.item_id, "quantity": 1},
            {"item_id": a.item_id, "quantity": 1},
            {"item_id": plain, "quantity": 1},
        ]
    )
    lines = [
        line_data(a, item_id=plain, unit=None, po_line_id=expected[1]["id"], quantity=2),
        line_data(
            a, item_id=substitute, unit=None, po_line_id=expected[2]["id"], condition="DAMAGED"
        ),
        line_data(a, po_line_id=expected[3]["id"], condition="OPENED"),
        line_data(a, po_line_id=expected[4]["id"]),
        line_data(a, item_id=plain, unit=None, po_line_id=expected[5]["id"], uom="M"),
        line_data(a, item_id=plain, unit=None, packing_quantity=2),
    ]
    lines[2]["unit"]["identifier"] = {
        "type": "MANUFACTURER_SERIAL",
        "value": None,
        "unreadable_reason": "Obscured",
    }
    lines[3]["unit"]["identifier"]["value"] = "SERIAL-001"
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            packing_reference="Packing list",
            lines=lines,
            comparator_ids=[expected[0]["id"]],
        ),
    )
    rows = {row["exception_type"]: row for row in result["exceptions"]}
    assert sorted(rows) == sorted(KINDS)
    return result, rows


def rejected(connection, tenant, statement, states=("23514", "23503", "23502")):
    """Assert the actual app login's failing transaction; the context manager rolls it back."""
    with pytest.raises(DBAPIError) as error:
        with connection.begin():
            set_authenticated(connection, tenant)
            connection.execute(statement)
    assert error.value.orig.sqlstate in states, str(error.value)


@pytest.mark.parametrize("kind", KINDS)
def test_each_type_rejects_missing_required_and_forbidden_nonnull_relationships(
    kind, matrix_receipt, asset_data, app_connection
):
    receipt, matrix = matrix_receipt
    a = asset_data[0]
    valid = {
        key: (UUID(value) if value is not None and key.endswith("_id") else value)
        for key, value in matrix[kind].items()
        if key not in {"id", "actor_id", "occurred_at", "recorded_at"}
    }
    required = ["receipt_id"]
    if kind in {"SHORT", "OVER", "SUBSTITUTION", "UOM_MISMATCH"}:
        required.append("po_line_id")
    if kind not in {"SHORT", "OVER"}:
        required.append("receipt_line_id")
    if kind == "SERIAL_UNREADABLE":
        required.append("asset_id")
    if kind == "SERIAL_MISMATCH":
        required.append("conflicting_asset_id")
    forbidden = ["conflicting_asset_id"] if kind != "SERIAL_MISMATCH" else ["asset_id"]
    if kind in {"SHORT", "OVER"}:
        forbidden += ["receipt_line_id", "asset_id"]
    if kind == "QUANTITY_VARIANCE":
        forbidden.append("asset_id")
    if kind == "UNEXPECTED_ITEM":
        forbidden.append("po_line_id")
    fillers = {
        "receipt_line_id": UUID(receipt["lines"][0]["id"]),
        "po_line_id": UUID(receipt["comparator_ids"][0]),
        "asset_id": a.asset_id,
        "conflicting_asset_id": a.asset_id,
    }
    for field in required:
        rejected(
            app_connection,
            a,
            receiving_exceptions.insert().values(valid | {"id": uuid7(), field: None}),
        )
    for field in forbidden:
        rejected(
            app_connection,
            a,
            receiving_exceptions.insert().values(valid | {"id": uuid7(), field: fillers[field]}),
        )
    # The valid shape must still fail as a duplicate, not as an invalid fact.
    rejected(
        app_connection,
        a,
        receiving_exceptions.insert().values(valid | {"id": uuid7()}),
        states=("23505",),
    )


@pytest.mark.parametrize(
    "attack",
    [
        "receipt",
        "same_po_line",
        "other_po_line",
        "asset",
        "conflict_owner",
        "cross_receipt",
        "cross_asset",
        "cross_conflict",
    ],
)
def test_matrix_rejects_same_tenant_stitching_and_foreign_relationships(
    attack, matrix_receipt, asset_data, receiving_client, make_order, app_connection
):
    receipt, rows = matrix_receipt
    a, b = asset_data
    other = post_receipt(receiving_client, a, receipt_data(a))
    foreign = post_receipt(receiving_client, b, receipt_data(b))
    _, expected = make_order()
    kind = "SERIAL_MISMATCH" if "conflict" in attack else "DAMAGED"
    values = exception_values(
        a,
        UUID(receipt["id"]),
        kind,
        receipt_line_id=UUID(rows[kind]["receipt_line_id"]),
        po_line_id=UUID(rows[kind]["po_line_id"]),
        conflicting_asset_id=a.asset_id if kind == "SERIAL_MISMATCH" else None,
    )
    changes = {
        "receipt": {"receipt_id": UUID(other["id"])},
        "same_po_line": {"po_line_id": UUID(rows["OPENED"]["po_line_id"])},
        "other_po_line": {"po_line_id": expected[0]["id"]},
        "asset": {"asset_id": a.asset_id},
        "conflict_owner": {"conflicting_asset_id": UUID(rows["SERIAL_UNREADABLE"]["asset_id"])},
        "cross_receipt": {"receipt_id": UUID(foreign["id"])},
        "cross_asset": {"asset_id": b.asset_id},
        "cross_conflict": {"conflicting_asset_id": b.asset_id},
    }
    rejected(app_connection, a, receiving_exceptions.insert().values(values | changes[attack]))


@pytest.mark.parametrize(
    "kind", ["DAMAGED", "OPENED", "SERIAL_UNREADABLE", "SERIAL_MISMATCH", "QUANTITY_VARIANCE"]
)
def test_conditional_po_reference_cannot_be_erased(
    kind, matrix_receipt, asset_data, app_connection
):
    _, rows = matrix_receipt
    # Quantity variance in this fixture is unexpected; giving it a comparator is equally invalid.
    row = rows[kind]
    values = exception_values(
        asset_data[0],
        UUID(row["receipt_id"]),
        kind,
        receipt_line_id=UUID(row["receipt_line_id"]),
        asset_id=UUID(row["asset_id"]) if row["asset_id"] else None,
        conflicting_asset_id=UUID(row["conflicting_asset_id"])
        if row["conflicting_asset_id"]
        else None,
        po_line_id=UUID(rows["DAMAGED"]["po_line_id"]) if kind == "QUANTITY_VARIANCE" else None,
    )
    rejected(app_connection, asset_data[0], receiving_exceptions.insert().values(values))


@pytest.mark.parametrize("condition", ["DAMAGED", "OPENED", "GOOD", "UNKNOWN"])
def test_sql_cannot_add_unsupported_condition_or_both_conditions(
    condition, receiving_client, space_data, make_item, app_connection
):
    a = space_data[0]
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(a, lines=[line_data(a, item_id=make_item(), unit=None, condition=condition)]),
    )
    for candidate in {"DAMAGED", "OPENED"} - {condition}:
        rejected(
            app_connection,
            a,
            receiving_exceptions.insert().values(
                exception_values(
                    a, UUID(result["id"]), candidate, receipt_line_id=UUID(result["lines"][0]["id"])
                )
            ),
        )


@pytest.mark.parametrize("table", RECEIVING_TABLES, ids=lambda table: table.name)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_every_committed_receiving_table_is_immutable(
    table, operation, matrix_receipt, asset_data, app_connection, migrator_connection
):
    a = asset_data[0]
    before = snapshot(migrator_connection, a.org_id)
    statement = {
        "UPDATE": f"UPDATE fleetops.{table.name} SET id=id",
        "DELETE": f"DELETE FROM fleetops.{table.name}",
        "TRUNCATE": f"TRUNCATE fleetops.{table.name}",
    }[operation]
    rejected(
        app_connection,
        a,
        text(statement),
        states=("23514",) if table is receipts and operation == "UPDATE" else ("42501",),
    )
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("table", RECEIVING_TABLES, ids=lambda table: table.name)
def test_rls_using_is_empty_without_scope_or_for_foreign_rows(
    table, matrix_receipt, asset_data, app_connection
):
    a, b = asset_data
    with app_connection.begin():
        assert app_connection.execute(select(table)).all() == []
    with app_connection.begin():
        set_authenticated(app_connection, b)
        assert app_connection.execute(select(table).where(table.c.org_id == a.org_id)).all() == []
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert app_connection.execute(select(table)).all()


@pytest.mark.parametrize(
    "mode", ["org_only", "invalid_credential", "foreign_credential", "spoof_context"]
)
@pytest.mark.parametrize("normal", [False, True])
def test_credential_is_required_at_both_ordinary_and_elevated_boundaries(
    mode, normal, receiving_client, asset_data, other_human, app_connection, migrator_connection
):
    a, b = asset_data
    result = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    before = snapshot(migrator_connection, a.org_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            if mode == "org_only":
                set_organization(app_connection, a.org_id)
            else:
                set_authenticated(app_connection, b if mode == "foreign_credential" else a)
                if mode == "foreign_credential":
                    set_organization(app_connection, a.org_id)
                if mode == "invalid_credential":
                    set_credential_context(app_connection, b"x" * 32)
                if mode == "spoof_context":
                    app_connection.execute(
                        text("SELECT set_config('fleetops.actor_id', :actor, true)"),
                        {"actor": str(other_human.actor_id)},
                    )
            if normal:
                app_connection.execute(text(UNIT_CALL), unit_parameters(a, UUID(result["id"])))
            else:
                app_connection.execute(
                    receipts.insert().values(
                        id=uuid7(),
                        org_id=a.org_id,
                        vendor_party_id=a.vendor_id,
                        dock_location_id=a.location_id,
                        received_at=receipt_data(a)["received_at"],
                    )
                )
    assert error.value.orig.sqlstate == "42501"
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("table", RECEIVING_TABLES, ids=lambda table: table.name)
def test_actor_and_record_times_cannot_be_supplied_via_sql(
    table, asset_data, other_human, app_connection
):
    rejected(
        app_connection,
        asset_data[0],
        table.insert().values(
            id=uuid7(),
            org_id=asset_data[0].org_id,
            actor_id=other_human.actor_id,
        ),
        states=("42501",),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"observed_identifier_type": "OTHER"},
        {"observed_identifier_value": "SERIAL-001"},
        {"observed_identifier_type": "INVALID", "observed_identifier_value": "SERIAL-001"},
        {"observed_identifier_type": "MANUFACTURER_SERIAL", "observed_identifier_value": " "},
        {"observed_identifier_type": "MANUFACTURER_SERIAL", "observed_identifier_value": "absent"},
    ],
)
def test_observed_serial_pair_must_be_valid_and_already_canonical(
    change, receiving_client, asset_data, app_connection
):
    a = asset_data[0]
    receipt = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    rejected(
        app_connection,
        a,
        receipt_lines.insert().values(
            id=uuid7(),
            org_id=a.org_id,
            receipt_id=UUID(receipt["id"]),
            item_id=a.item_id,
            quantity=1,
            uom="EA",
            condition="GOOD",
            owner_party_id=a.party_id,
            **change,
        ),
    )


def test_canonical_existing_asset_cannot_be_attached_as_received_identity(
    receiving_client, asset_data, app_connection
):
    a = asset_data[0]
    receipt = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    rejected(
        app_connection,
        a,
        receipt_lines.insert().values(
            id=uuid7(),
            org_id=a.org_id,
            receipt_id=UUID(receipt["id"]),
            item_id=a.item_id,
            quantity=1,
            uom="EA",
            condition="GOOD",
            owner_party_id=a.party_id,
            asset_id=a.asset_id,
            observed_identifier_type="MANUFACTURER_SERIAL",
            observed_identifier_value="SERIAL-001",
        ),
        states=("42501",),
    )


@pytest.mark.parametrize("missing", ["normal_exception", "known_exception", "quantity_exception"])
def test_omitted_required_exception_rejects_at_commit_and_rolls_back_whole_transaction(
    missing, receiving_client, asset_data, make_order, app_connection, migrator_connection
):
    a = asset_data[0]
    po, expected = make_order()
    receipt = post_receipt(
        receiving_client,
        a,
        receipt_data(a, po_id=po["id"], comparator_ids=[expected[0]["id"]], reconcile=False),
    )
    before = snapshot(migrator_connection, a.org_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            if missing == "quantity_exception":
                app_connection.execute(
                    receipt_reconciliations.insert().values(
                        id=uuid7(), org_id=a.org_id, receipt_id=UUID(receipt["id"])
                    )
                )
            elif missing == "normal_exception":
                app_connection.execute(
                    text(UNIT_CALL),
                    unit_parameters(
                        a, UUID(receipt["id"]), po_line_id=expected[0]["id"], condition="DAMAGED"
                    ),
                )
            else:
                app_connection.execute(
                    receipt_lines.insert().values(
                        id=uuid7(),
                        org_id=a.org_id,
                        receipt_id=UUID(receipt["id"]),
                        po_line_id=expected[0]["id"],
                        item_id=a.item_id,
                        quantity=1,
                        uom="EA",
                        condition="GOOD",
                        owner_party_id=a.party_id,
                        observed_identifier_type="MANUFACTURER_SERIAL",
                        observed_identifier_value="SERIAL-001",
                    )
                )
    assert error.value.orig.sqlstate == "23514"
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize(
    "component",
    [
        "assets",
        "asset_transitions",
        "asset_initial_facts",
        "asset_initial_assignment_facts",
        "asset_identifiers",
        "receipt_lines",
        "receiving_exceptions",
    ],
)
def test_failure_of_each_required_creation_component_rolls_back_entire_bundle(
    component, receiving_client, space_data, migrator_connection
):
    a = space_data[0]
    before = snapshot(migrator_connection, a.org_id)
    # A temporary owner-installed rejection simulates each required write failing.
    # The operation itself still uses ordinary production credentials and API code.
    with migrator_connection.begin():
        migrator_connection.exec_driver_sql("""
            CREATE FUNCTION fleetops.slice9_test_reject() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='Injected required-write failure';
            END
            $$""")
        migrator_connection.exec_driver_sql(
            f"CREATE TRIGGER slice9_test_reject BEFORE INSERT ON fleetops.{component} "
            "FOR EACH ROW EXECUTE FUNCTION fleetops.slice9_test_reject()"
        )
    try:
        response = receiving_client.post(
            "/receipts",
            headers=headers(a),
            json=json_data(receipt_data(a, lines=[line_data(a, condition="DAMAGED")])),
        )
        assert response.status_code == 422
        assert snapshot(migrator_connection, a.org_id) == before
    finally:
        migrator_connection.rollback()
        with migrator_connection.begin():
            migrator_connection.exec_driver_sql(
                f"DROP TRIGGER slice9_test_reject ON fleetops.{component}"
            )
            migrator_connection.exec_driver_sql("DROP FUNCTION fleetops.slice9_test_reject()")


def test_normal_boundary_direct_duplicate_rolls_back_without_observation_fallback(
    receiving_client, asset_data, app_connection, migrator_connection
):
    a = asset_data[0]
    receipt = post_receipt(receiving_client, a, receipt_data(a, reconcile=False))
    before = snapshot(migrator_connection, a.org_id)
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_authenticated(app_connection, a)
            app_connection.execute(
                text(UNIT_CALL),
                unit_parameters(a, UUID(receipt["id"]), identifier_value="SERIAL-001"),
            )
    assert error.value.orig.sqlstate == "23505"
    assert snapshot(migrator_connection, a.org_id) == before
