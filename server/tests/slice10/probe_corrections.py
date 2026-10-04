"""Independent numbered PostgreSQL probes, separate from pytest test cases.

Run with python -m server.tests.slice10.probe_corrections. Reuse disposable-cluster
and fixture-input helpers only. HTTP is the tested workflow; raw psycopg reads and
actual runtime SQL privileges provide independent persisted-state assertions.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from server.tests.conftest import disposable_database
from server.tests.slice9.probe_receiving import (
    ASSET_TABLES,
    WHEN,
    auth,
    body,
    connect,
    line,
    order,
    request,
    rows,
    seed,
)

from fleetops.api.app import create_app

PASSED = set()


def mark(number, description):
    assert number not in PASSED
    PASSED.add(number)
    print(f"PASS {number:02}: {description}", flush=True)


def get(client, tenant, path, **params):
    response = client.get(
        path, headers={"Authorization": "Bearer " + tenant["token"]}, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def correction(client, tenant, path, expected=201, **changes):
    return request(
        client,
        tenant,
        path,
        dict(reason="Independent correction", correction_occurred_at=WHEN, **changes),
        expected=expected,
    )


def main():
    cluster = disposable_database()
    database = next(cluster)
    try:
        with (
            connect(database, "fleetops_migrator") as owner,
            connect(database, "fleetops_app") as app,
        ):
            version = owner.execute("SHOW server_version").fetchone()[0]
            assert version.startswith("16.15"), version
            print("PostgreSQL " + version, flush=True)
            tenant, foreign = seed(owner), seed(owner)
            with TestClient(create_app(database.settings(tenant["org"]))) as client:
                asset_probes(database, owner, app, client, tenant, foreign)
                receipt_probes(owner, app, client, tenant)
                security_probes(owner, app, client, tenant, foreign)
            assert PASSED == set(range(1, 41)), sorted(set(range(1, 41)) - PASSED)
    finally:
        cluster.close()
    print("40/40 independent probes PASS; disposable cluster removed.", flush=True)


def asset_probes(database, owner, app, client, tenant, foreign):
    capture = request(
        client,
        tenant,
        "/receipts",
        body(tenant, [line(tenant, actual_item=tenant["item"], serialized=True)]),
    )
    asset = capture["lines"][0]["asset_id"]
    destination_b, destination_c = uuid4(), uuid4()
    for location, label in ((destination_b, "B"), (destination_c, "C")):
        owner.execute(
            """INSERT INTO fleetops.locations
          (id,org_id,facility_id,code,name,kind,created_by_actor_id)
          VALUES (%s,%s,%s,%s,%s,'BIN',%s)""",
            (location, tenant["org"], tenant["facility"], label, label, tenant["actor"]),
        )
    base = f"/assets/{asset}"
    root = request(
        client,
        tenant,
        base + "/movements",
        dict(
            expected_version=1,
            to_location_id=str(destination_b),
            reason="Mis-keyed movement",
            occurred_at=WHEN,
        ),
    )
    original = owner.execute(
        "SELECT * FROM fleetops.asset_movements WHERE id=%s", (root["id"],)
    ).fetchone()
    url = base + f"/movements/{root['id']}/corrections"
    request(
        client,
        tenant,
        url,
        dict(expected_version=2, to_location_id=str(destination_c), correction_occurred_at=WHEN),
        expected=422,
    )
    assert (
        owner.execute(
            "SELECT count(*) FROM fleetops.asset_movements WHERE asset_id=%s", (asset,)
        ).fetchone()[0]
        == 1
    )
    mark(4, "missing reason rejects without writes")
    first = correction(
        client, tenant, url, expected_version=2, to_location_id=str(destination_c), occurred_at=WHEN
    )
    raw = owner.execute(
        """SELECT correction_role,result_version,from_location_id,to_location_id,
        corrects_movement_id,correction_pair_id,correction_generation,actor_id
        FROM fleetops.asset_movements WHERE asset_id=%s ORDER BY result_version""",
        (asset,),
    ).fetchall()
    assert [r[0] for r in raw] == ["NONE", "REVERSAL", "CORRECTED"]
    mark(1, "first movement creates exactly three raw roles")
    assert (
        owner.execute(
            "SELECT * FROM fleetops.asset_movements WHERE id=%s", (root["id"],)
        ).fetchone()
        == original
    )
    mark(2, "original movement byte-for-byte unchanged")
    assert [r[1] for r in raw] == [2, 3, 4]
    assert owner.execute(
        "SELECT version,current_location_id FROM fleetops.assets WHERE id=%s", (asset,)
    ).fetchone() == (4, destination_c)
    mark(3, "N+1/N+2 and projection commit together")
    assert raw[1][7] == raw[2][7] == tenant["actor"]
    mark(5, "correction Actor is credential-derived")
    assert [(r[2], r[3]) for r in raw] == [
        (tenant["dock"], destination_b),
        (tenant["dock"], destination_b),
        (tenant["dock"], destination_c),
    ]
    assert raw[1][5] == raw[2][5] and raw[1][6] == raw[2][6] == 1
    assert owner.execute(
        "SELECT id FROM fleetops.effective_asset_movements WHERE asset_id=%s", (asset,)
    ).fetchall() == [(UUID(first["id"]),)]
    mark(40, "exact A to B corrected A to C; no physical inverse claim")
    second = correction(client, tenant, url, expected_version=4, to_location_id=str(destination_b))
    assert second["correction_generation"] == 2 and second["result_version"] == 6
    assert (
        owner.execute(
            "SELECT count(*) FROM fleetops.asset_movements WHERE asset_id=%s", (asset,)
        ).fetchone()[0]
        == 5
    )
    mark(6, "repeat correction advances generation and global version")
    assert second["corrects_movement_id"] == root["id"]
    assert (
        owner.execute(
            "SELECT count(DISTINCT corrects_movement_id) FROM fleetops.asset_movements "
            "WHERE asset_id=%s AND correction_role<>'NONE'",
            (asset,),
        ).fetchone()[0]
        == 1
    )
    mark(7, "all generations retain ordinary root")
    custody = request(
        client,
        tenant,
        base + "/custody-changes",
        dict(
            expected_version=6,
            to_custodian_party_id=str(tenant["vendor"]),
            reason="Custody starts",
            occurred_at=WHEN,
        ),
    )
    correction(client, tenant, url, expected=409, expected_version=7, to_location_id=None)
    assert (
        owner.execute("SELECT version FROM fleetops.assets WHERE id=%s", (asset,)).fetchone()[0]
        == 7
    )
    mark(8, "later unrelated global event blocks repeat without replay")
    result = correction(
        client,
        tenant,
        base + f"/custody-changes/{custody['id']}/corrections",
        expected_version=7,
        to_custodian_party_id=None,
    )
    assert result["from_custodian_party_id"] is None and result["to_custodian_party_id"] is None
    assert get(client, tenant, base)["custodian_party_id"] is None
    mark(12, "custody correction allows NULL and preserves owner")
    owned = request(
        client,
        tenant,
        base + "/ownership-changes",
        dict(
            expected_version=9,
            to_owner_party_id=str(tenant["vendor"]),
            reason="Owner mistake",
            occurred_at=WHEN,
        ),
    )
    correction(
        client,
        tenant,
        base + f"/ownership-changes/{owned['id']}/corrections",
        expected_version=10,
        to_owner_party_id=str(tenant["maker"]),
    )
    assert get(client, tenant, base)["owner_party_id"] == str(tenant["maker"])
    mark(11, "ownership replacement updates only represented title and global version")
    assigned = request(
        client,
        tenant,
        base + "/assignments",
        dict(
            expected_version=12,
            assignee_type="ACTOR",
            assignee_id=str(tenant["actor"]),
            reason="Assignment",
            occurred_at=WHEN,
        ),
    )
    corrected = correction(
        client,
        tenant,
        base + f"/assignments/{assigned['id']}/corrections",
        expected_version=13,
        to_assignee_type="LOCATION",
        to_assignee_id=str(destination_c),
    )
    assert get(client, tenant, base)["current_assignment_id"] == corrected["id"]
    mark(13, "assignment correction supplies effective establishing event")
    history = get(client, tenant, base + "/assignments")
    assert len(history["events"]) == 3 and len(history["intervals"]) == 1
    assert history["intervals"][0]["establishing_event_id"] == corrected["id"]
    assert history["intervals"][0]["assignee_id"] == str(destination_c)
    mark(14, "effective interval excludes reversal and superseded root")
    transition = request(
        client,
        tenant,
        base + "/transitions",
        dict(
            expected_version=15,
            from_state="RECEIVED",
            to_state="IN_STOCK",
            reason="State",
            occurred_at=WHEN,
        ),
    )
    correction(
        client,
        tenant,
        base + f"/transitions/{transition['id']}/corrections",
        expected=422,
        expected_version=16,
        to_state="DEPLOYED",
    )
    correction(
        client,
        tenant,
        base + f"/transitions/{transition['id']}/corrections",
        expected_version=16,
        to_state="ON_HOLD",
    )
    assert get(client, tenant, base)["current_state"] == "ON_HOLD"
    mark(10, "lifecycle replacement obeys effective pre-root graph")
    racer = request(
        client,
        tenant,
        base + "/movements",
        dict(
            expected_version=18,
            to_location_id=str(destination_c),
            reason="Race root",
            occurred_at=WHEN,
        ),
    )
    barrier = Barrier(2)

    def contend(destination):
        try:
            with connect(database, "fleetops_app") as connection, connection.transaction():
                auth(connection, tenant)
                barrier.wait(timeout=10)
                connection.execute(
                    """SELECT * FROM fleetops.correct_movement(
                  %s,%s,19,%s,'Concurrent correction',%s,NULL,%s,%s,%s)""",
                    (asset, racer["id"], destination, WHEN, uuid4(), uuid4(), uuid4()),
                )
            return "committed"
        except psycopg.Error as error:
            return error.sqlstate

    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(contend, [destination_b, destination_c]))
    assert outcomes.count("committed") == 1 and len(set(outcomes)) == 2, outcomes
    assert (
        owner.execute("SELECT version FROM fleetops.assets WHERE id=%s", (asset,)).fetchone()[0]
        == 21
    )
    mark(9, "independent runtime contenders produce exactly one correction winner")
    assert get(client, tenant, "/health/assets/reconciliation") == []
    assert get(client, tenant, "/health/corrections") == []
    mark(34, "valid corrected raw and effective authority passes health")
    # Deliberate owner-only corruption proves health does not infer or repair authority.
    owner.execute("ALTER TABLE fleetops.asset_movements DISABLE TRIGGER correction_pair_complete")
    try:
        owner.execute(
            "UPDATE fleetops.asset_movements SET reason='Mismatched member' WHERE id=%s",
            (first["id"],),
        )
        assert get(client, tenant, "/health/corrections")
        assert (
            owner.execute(
                "SELECT reason FROM fleetops.asset_movements WHERE id=%s", (first["id"],)
            ).fetchone()[0]
            == "Mismatched member"
        )
        mark(35, "malformed older correction is reported without repair")
    finally:
        owner.execute(
            "UPDATE fleetops.asset_movements SET reason='Independent correction' WHERE id=%s",
            (first["id"],),
        )
        owner.execute(
            "ALTER TABLE fleetops.asset_movements ENABLE TRIGGER correction_pair_complete"
        )


def receipt_probes(owner, app, client, tenant):
    po, targets = order(app, tenant, [(tenant["plain"], 2)])
    po_url = f"/purchase-orders/{po}/lines/{targets[0]}/corrections"
    original = owner.execute(
        "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (targets[0],)
    ).fetchone()
    corrected = correction(client, tenant, po_url, expected_generation=0, quantity="3")
    assert corrected["correction_generation"] == 1
    assert (
        owner.execute(
            "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (targets[0],)
        ).fetchone()
        == original
    )
    mark(18, "unreferenced issued procurement mistake corrects without original mutation")
    stale = body(tenant, po=po, comparators=targets)
    request(client, tenant, "/receipts", stale, expected=409)
    mark(21, "stale comparator correction generation rejects")
    stale["comparator_ids"] = []
    stale["comparator_bindings"] = [dict(po_line_id=str(targets[0]), expected_generation=1)]
    pinned = request(client, tenant, "/receipts", stale)
    assert owner.execute(
        "SELECT source_generation,source_id FROM fleetops.receipt_comparators WHERE receipt_id=%s",
        (pinned["id"],),
    ).fetchone() == (1, UUID(corrected["id"]))
    mark(20, "new receipt explicitly binds corrected expectation source")
    correction(client, tenant, po_url, expected=409, expected_generation=1, quantity="4")
    mark(19, "bound comparator cannot be reinterpreted")
    quantity_po, quantity_targets = order(app, tenant, [(tenant["plain"], 4)])
    for index, status in enumerate(("OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"), 22):
        captured = request(
            client,
            tenant,
            "/receipts",
            body(
                tenant,
                [line(tenant, comparator=quantity_targets[0], quantity=4, condition="DAMAGED")],
                po=quantity_po,
            ),
        )
        root = captured["lines"][0]
        observation = captured["exceptions"][0]
        event_url = f"/exceptions/{observation['id']}"
        if status != "OPEN":
            request(
                client,
                tenant,
                event_url + "/events",
                dict(
                    expected_status="OPEN",
                    to_status=status,
                    note="Prior disposition",
                    occurred_at=WHEN,
                ),
            )
        before = get(client, tenant, event_url)
        response = correction(
            client,
            tenant,
            f"/receipts/{captured['id']}/lines/{root['id']}/corrections",
            expected_generation=0,
            condition="GOOD",
        )
        after = get(client, tenant, event_url)
        if status in ("RESOLVED", "WAIVED"):
            assert before == after
        else:
            assert (
                after["status"] == "RESOLVED" and len(after["events"]) == len(before["events"]) + 1
            )
            assert after["events"][-1]["evaluation_id"] is not None
        assert get(client, tenant, f"/receipts/{captured['id']}")["lines"][0] == root
        mark(index, status + " false-observation correction consequence")
        if index == 22:
            assert response["condition"] == "GOOD"
            mark(
                15,
                "nonserialized correction preserves original capture and supplies effective values",
            )
        if index == 25:
            request(
                client,
                tenant,
                event_url + "/events",
                dict(
                    expected_status="WAIVED",
                    to_status="RESOLVED",
                    note="Forbidden",
                    occurred_at=WHEN,
                ),
                expected=422,
            )
            assert get(client, tenant, event_url) == after
            mark(26, "terminal direct workflow transition rejects")
    original_local = request(
        client,
        tenant,
        "/receipts",
        body(tenant, [line(tenant, comparator=quantity_targets[0], quantity=4)], po=quantity_po),
    )
    other = request(
        client,
        tenant,
        "/receipts",
        body(tenant, [line(tenant, comparator=quantity_targets[0], quantity=1000)], po=quantity_po),
    )
    local_url = (
        f"/receipts/{original_local['id']}/lines/{original_local['lines'][0]['id']}/corrections"
    )
    correction(client, tenant, local_url, expected_generation=0, quantity="2")
    opened = get(client, tenant, "/exceptions/open", receipt_id=original_local["id"])
    assert [e["exception_type"] for e in opened] == ["SHORT"] and opened[0]["evaluation_id"]
    mark(27, "newly true disagreement creates immutable OPEN instance")
    assert owner.execute(
        "SELECT receipt_id FROM fleetops.receiving_exceptions WHERE id=%s", (opened[0]["id"],)
    ).fetchone()[0] == UUID(original_local["id"])
    mark(28, "corrected reconciliation uses the same receipt and comparator")
    assert get(client, tenant, f"/receipts/{other['id']}") == other
    mark(30, "other receipt excess never nets with corrected shortage")
    correction(client, tenant, local_url, expected_generation=1, uom="M", quantity="1000")
    assert [
        e["exception_type"]
        for e in get(client, tenant, "/exceptions/open", receipt_id=original_local["id"])
    ] == ["UOM_MISMATCH"]
    mark(29, "mixed-token population blocks quantity comparison")
    physical = line(tenant, actual_item=tenant["item"], serialized=True)
    captured = request(client, tenant, "/receipts", body(tenant, [physical]))
    asset_snapshot = rows(owner, tenant, ASSET_TABLES)
    correction(
        client,
        tenant,
        f"/receipts/{captured['id']}/lines/{captured['lines'][0]['id']}/corrections",
        expected=422,
        expected_generation=0,
        quantity="2",
    )
    assert rows(owner, tenant, ASSET_TABLES) == asset_snapshot
    mark(16, "serialized creation-changing correction rejects without Asset writes")
    conflict = request(
        client,
        tenant,
        "/receipts",
        body(
            tenant,
            [
                line(
                    tenant,
                    actual_item=tenant["item"],
                    serialized=True,
                    serial=physical["unit"]["identifier"]["value"],
                )
            ],
        ),
    )
    assert conflict["lines"][0]["asset_id"] is None
    correction(
        client,
        tenant,
        f"/receipts/{conflict['id']}/lines/{conflict['lines'][0]['id']}/corrections",
        expected=422,
        expected_generation=0,
        observed_identifier_type=None,
        observed_identifier_value=None,
    )
    assert rows(owner, tenant, ASSET_TABLES) == asset_snapshot
    mark(17, "serial conflict cannot become retroactive normal Asset creation")


def security_probes(owner, app, client, tenant, foreign):
    tables = [
        "purchase_order_line_corrections",
        "receipt_line_corrections",
        "receipt_correction_evaluations",
        "receipt_evaluation_lines",
        "receipt_evaluation_expectations",
        "receipt_evaluation_exceptions",
        "exception_workflows",
        "exception_events",
    ]
    for table in tables:
        assert (
            owner.execute(
                sql.SQL("SELECT count(*) FROM fleetops.{} WHERE org_id=%s").format(
                    sql.Identifier(table)
                ),
                (tenant["org"],),
            ).fetchone()[0]
            > 0
        )
        for context in (None, foreign):
            with app.transaction():
                if context:
                    auth(app, context)
                assert (
                    app.execute(
                        sql.SQL("SELECT count(*) FROM fleetops.{}").format(sql.Identifier(table))
                    ).fetchone()[0]
                    == 0
                )
    mark(31, "all eight populated new tables fail closed with absent or foreign tenant")
    for table in [
        "exception_events",
        "receipt_line_corrections",
        "purchase_order_line_corrections",
        "asset_movements",
    ]:
        for operation in ("UPDATE", "DELETE", "TRUNCATE"):
            statement = (
                sql.SQL("UPDATE fleetops.{} SET org_id=org_id")
                if operation == "UPDATE"
                else sql.SQL("DELETE FROM fleetops.{}")
                if operation == "DELETE"
                else sql.SQL("TRUNCATE fleetops.{}")
            )
            try:
                with app.transaction():
                    auth(app, tenant)
                    app.execute(statement.format(sql.Identifier(table)))
            except psycopg.Error as error:
                assert error.sqlstate == "42501", error
            else:
                raise AssertionError((table, operation, "unexpected authority"))
    mark(32, "exception_events rejects runtime UPDATE DELETE TRUNCATE")
    mark(33, "new and original correction histories reject runtime mutation")
    paths = client.get("/openapi.json").json()["paths"]
    assert "/corrections" not in paths
    mark(36, "no unrestricted generic correction API")
    assert not any("corrections" in p and "identifier" in p for p in paths)
    mark(37, "canonical identifier correction remains absent")
    assert not any("corrections" in p and "configuration" in p for p in paths)
    mark(38, "configuration correction remains absent")
    assert not any("conversion" in p for p in paths)
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert not any(
        "conversion" in key
        for name in ("ReceiptLineCorrection", "ProcurementCorrection")
        for key in schemas[name]["properties"]
    )
    mark(39, "no conversion route or authority field")


if __name__ == "__main__":
    main()
