"""Exact-token UOM and receipt-local arithmetic, including adversarial precision and grouping."""

from decimal import Decimal

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice9.conftest import (
    headers,
    json_data,
    kinds,
    line_data,
    post_receipt,
    receipt_data,
    snapshot,
)

from fleetops.db.metadata import items, purchase_order_lines, purchase_orders
from fleetops.domain import procurement

UOMS = ("EA", "M", "MM", "CM", "IN", "FT", "G", "MG", "KG", "ML", "L")


@pytest.mark.parametrize("uom", UOMS)
def test_existing_uom_tokens_compare_directly_without_default_normalization(
    uom,
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item(uom=uom)
    po, expected = make_order(specs=[{"item_id": item, "quantity": "1.25"}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    quantity="1.25",
                    uom=uom,
                ),
            ],
        ),
    )
    assert result["exceptions"] == []
    assert result["lines"][0]["uom"] == uom
    assert Decimal(result["lines"][0]["quantity"]) == Decimal("1.25")


@pytest.mark.parametrize("actual", ["CM", "MM", "FT", "EA"])
@pytest.mark.parametrize("quantity", ["0.1", "10", "10000"])
def test_mismatched_units_signal_incomparability_without_any_quantity_conclusion(
    actual,
    quantity,
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item(uom="M")
    po, expected = make_order(specs=[{"item_id": item, "quantity": "10"}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    quantity=quantity,
                    uom=actual,
                ),
            ],
        ),
    )
    assert kinds(result) == ["UOM_MISMATCH"]
    line, error = result["lines"][0], result["exceptions"][0]
    assert line["uom"] == actual and Decimal(line["quantity"]) == Decimal(quantity)
    assert error["receipt_line_id"] == line["id"]
    assert error["po_line_id"] == str(expected[0]["id"])
    assert error["asset_id"] is error["conflicting_asset_id"] is None


@pytest.mark.parametrize("subtotal", ["4", "10", "14"])
def test_any_mixed_uom_blocks_local_total_even_if_comparable_subtotal_reaches_expectation(
    subtotal,
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item(uom="M")
    po, expected = make_order(specs=[{"item_id": item, "quantity": "10"}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    quantity=subtotal,
                    uom="M",
                ),
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    quantity="300",
                    uom="CM",
                ),
            ],
        ),
    )
    assert kinds(result) == ["UOM_MISMATCH"]


def test_cross_receipt_quantities_never_accumulate_or_rewrite_earlier_exception(
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": "10"}])
    results = [
        post_receipt(
            receiving_client,
            a,
            receipt_data(
                a,
                po_id=po["id"],
                lines=[
                    line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=q),
                ],
            ),
        )
        for q in ("8", "4")
    ]
    assert [kinds(r) for r in results] == [["SHORT"], ["SHORT"]]
    again = receiving_client.get(f"/receipts/{results[0]['id']}", headers=headers(a))
    assert again.status_code == 200 and again.json() == results[0]


def test_mismatch_blocks_only_its_exact_comparator_and_its_own_receipt(
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(
        specs=[
            {"item_id": item, "quantity": 2},
            {"item_id": item, "quantity": 2},
        ]
    )
    r1 = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], uom="CM"),
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], uom="MM"),
                line_data(a, item_id=item, unit=None, po_line_id=expected[1]["id"]),
            ],
        ),
    )
    assert kinds(r1) == ["SHORT", "UOM_MISMATCH", "UOM_MISMATCH"]
    assert {
        error["receipt_line_id"]
        for error in r1["exceptions"]
        if error["exception_type"] == "UOM_MISMATCH"
    } == {line["id"] for line in r1["lines"] if line["po_line_id"] == str(expected[0]["id"])}
    short = next(e for e in r1["exceptions"] if e["exception_type"] == "SHORT")
    assert short["po_line_id"] == str(expected[1]["id"])
    r2 = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"]),
            ],
        ),
    )
    assert kinds(r2) == ["SHORT"]


@pytest.mark.parametrize("mismatched", [False, True])
def test_substituted_item_remains_in_same_comparator_population(
    mismatched,
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    expected_item, actual_item = make_item(), make_item(uom="M")
    po, expected = make_order(specs=[{"item_id": expected_item, "quantity": 2}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=expected_item, unit=None, po_line_id=expected[0]["id"]),
                line_data(
                    a,
                    item_id=actual_item,
                    unit=None,
                    po_line_id=expected[0]["id"],
                    uom="M" if mismatched else "EA",
                ),
            ],
        ),
    )
    assert kinds(result) == (["SUBSTITUTION", "UOM_MISMATCH"] if mismatched else ["SUBSTITUTION"])


def test_empty_included_comparator_creates_short_without_a_fictional_receipt_line(
    receiving_client,
    space_data,
    make_order,
):
    a = space_data[0]
    po, expected = make_order()
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            comparator_ids=[expected[0]["id"]],
        ),
    )
    assert result["lines"] == [] and kinds(result) == ["SHORT"]
    assert result["exceptions"][0]["receipt_line_id"] is None


def test_reconciliation_does_not_round_arbitrary_precision_numeric_observations(
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item()
    exact = "10000000000000000000000000000.000000000000001"
    po, expected = make_order(specs=[{"item_id": item, "quantity": exact}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"], quantity=exact),
            ],
        ),
    )
    assert Decimal(result["lines"][0]["quantity"]) == Decimal(exact)
    assert result["exceptions"] == []


def test_item_default_edits_and_later_supersession_preserve_admitted_comparator_and_actual_uom(
    receiving_client,
    space_data,
    make_item,
    make_order,
    migrator_connection,
    app_connection,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 1}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"],
            lines=[
                line_data(a, item_id=item, unit=None, po_line_id=expected[0]["id"]),
            ],
        ),
    )
    before = snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines))
    with app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.execute(items.update().where(items.c.id == item).values(uom="M"))
    assert (
        snapshot(migrator_connection, a.org_id, (purchase_orders, purchase_order_lines)) == before
    )
    with app_connection.begin():
        set_authenticated(app_connection, a)
        successor = procurement.create_line(
            app_connection,
            po["id"],
            predecessor_id=expected[0]["id"],
            performer_id=a.actor_id,
            values={"item_id": item, "quantity": 2, "unit_price": 3, "expected_date": None},
        )
    assert successor["uom"] == "M"
    assert receiving_client.get(f"/receipts/{result['id']}", headers=headers(a)).json() == result
    assert result["lines"][0]["uom"] == "EA"


@pytest.mark.parametrize("path", ["header", "line", "unit", "identifier"])
@pytest.mark.parametrize(
    "field",
    [
        "conversion_factor",
        "normalized_quantity",
        "equivalent_quantity",
        "canonical_uom",
        "normalized_uom",
        "units_per_package",
    ],
)
def test_conversion_authority_fields_are_rejected_at_every_input_boundary(
    path,
    field,
    receiving_client,
    space_data,
    make_order,
    migrator_connection,
):
    a = space_data[0]
    po, expected = make_order()
    body = receipt_data(a, po_id=po["id"], lines=[line_data(a, po_line_id=expected[0]["id"])])
    targets = {
        "header": body,
        "line": body["lines"][0],
        "unit": body["lines"][0]["unit"],
        "identifier": body["lines"][0]["unit"]["identifier"],
    }
    targets[path][field] = 100
    before = snapshot(migrator_connection, a.org_id)
    response = receiving_client.post("/receipts", headers=headers(a), json=json_data(body))
    assert response.status_code == 422
    assert snapshot(migrator_connection, a.org_id) == before


@pytest.mark.parametrize("po_backed", [False, True])
@pytest.mark.parametrize("packing", [None, "1", "2"])
def test_packing_variance_is_line_scoped_and_independent_of_po_quantity(
    po_backed,
    packing,
    receiving_client,
    space_data,
    make_item,
    make_order,
):
    a = space_data[0]
    item = make_item()
    po, expected = make_order(specs=[{"item_id": item, "quantity": 1}])
    result = post_receipt(
        receiving_client,
        a,
        receipt_data(
            a,
            po_id=po["id"] if po_backed else None,
            packing_reference="Packing slip 1",
            lines=[
                line_data(
                    a,
                    item_id=item,
                    unit=None,
                    po_line_id=expected[0]["id"] if po_backed else None,
                    packing_quantity=packing,
                )
            ],
        ),
    )
    expected_types = ([] if po_backed else ["UNEXPECTED_ITEM"]) + (
        ["QUANTITY_VARIANCE"] if packing == "2" else []
    )
    assert kinds(result) == sorted(expected_types)
    for exception in result["exceptions"]:
        if exception["exception_type"] == "QUANTITY_VARIANCE":
            assert exception["receipt_line_id"] == result["lines"][0]["id"]
            assert exception["asset_id"] is exception["conflicting_asset_id"] is None
            assert exception["po_line_id"] == (str(expected[0]["id"]) if po_backed else None)
