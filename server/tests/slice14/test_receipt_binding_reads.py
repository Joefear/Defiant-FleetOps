"""Receipt reads expose acknowledged sources and preserve procurement freezing."""

from server.tests.slice5.conftest import headers


def test_receipt_comparator_reads_preserve_acknowledged_generations(
    asset_client,
    asset_data,
    make_order,
):
    tenant = asset_data[0]
    for generation in (0, 1):
        order, lines = make_order()
        line = lines[0]
        route = f"/purchase-orders/{order['id']}/lines/{line['id']}"

        def correct(expected_generation, quantity, correction_route=route):
            return asset_client.post(
                correction_route + "/corrections",
                headers=headers(tenant),
                json={
                    "expected_generation": expected_generation,
                    "quantity": quantity,
                    "reason": "Expectation read proof",
                    "correction_occurred_at": "2026-10-04T12:00:00Z",
                },
            )

        source = None
        if generation == 1:
            correction = correct(0, "2")
            assert correction.status_code == 201, correction.text
            effective = asset_client.get(route + "/effective", headers=headers(tenant))
            assert effective.status_code == 200, effective.text
            source = effective.json()["source_id"]

        receipt = asset_client.post(
            "/receipts",
            headers=headers(tenant),
            json={
                "vendor_party_id": str(tenant.vendor_id),
                "po_id": str(order["id"]),
                "dock_location_id": str(tenant.location_id),
                "received_at": "2026-10-04T12:00:00Z",
                "reconcile": False,
                "comparator_bindings": [
                    {"po_line_id": str(line["id"]), "expected_generation": generation}
                ],
            },
        )
        assert receipt.status_code == 201, receipt.text
        blocked = correct(generation, "3")
        assert blocked.status_code == 409, blocked.text
        assert "Receipt-bound procurement facts are frozen" in blocked.text

        response = asset_client.get(f"/receipts/{receipt.json()['id']}", headers=headers(tenant))
        assert response.status_code == 200, response.text
        assert response.json()["comparator_bindings"] == [
            {
                "po_line_id": str(line["id"]),
                "source_generation": generation,
                "source_id": source,
            }
        ]
