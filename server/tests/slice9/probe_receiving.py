"""Independent A-AG probes: HTTP workflows plus raw psycopg oracles and blocked SQL races.

Run with python -m server.tests.slice9.probe_receiving. Only the disposable-cluster
harness is reused. No receiving service, SQLAlchemy model, or pytest assertion helper
is imported. The production API is the system under test, not the expected-result oracle.
"""

import secrets
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from queue import Queue
from time import monotonic, sleep
from uuid import UUID, uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from server.tests.conftest import disposable_database

from fleetops.api.app import create_app
from fleetops.auth import hash_password, token_digest

ASSET_TABLES = [
    "assets",
    "asset_identifiers",
    "asset_transitions",
    "asset_initial_facts",
    "asset_initial_assignment_facts",
    "asset_movements",
    "asset_custody_changes",
    "asset_ownership_changes",
    "asset_assignment_events",
    "asset_configurations",
]
RECEIVING_TABLES = [
    "receipts",
    "receipt_comparators",
    "receipt_lines",
    "receipt_reconciliations",
    "receiving_exceptions",
]
WHEN = "2026-09-11T15:00:00Z"
UNIT_SQL = """
SELECT * FROM fleetops.create_received_unit(
    %(receipt)s,%(line)s,NULL,%(item)s,1,'EA','GOOD',NULL,NULL,%(asset)s,%(tag)s,
    'Independent unit',%(owner)s,NULL,%(identifier)s,'MANUFACTURER_SERIAL',
    %(serial)s,NULL,%(transition)s)
"""
PASSED = set()


def mark(code, description):
    """Emit a result only after the corresponding independent assertions succeeded."""
    assert code not in PASSED
    PASSED.add(code)
    print(f"PASS {code}: {description}", flush=True)


def connect(database, role):
    """Use separate real PostgreSQL login roles; never elevate runtime requests."""
    url = database.url(role)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        autocommit=True,
    )


def auth(connection, tenant):
    connection.execute("SELECT set_config('fleetops.org_id', %s, true)", (str(tenant["org"]),))
    connection.execute(
        "SELECT set_config('fleetops.session_digest_hex', %s, true)",
        (token_digest(tenant["token"]).hex(),),
    )


def seed(owner):
    """Explicit administrative fixture data; runtime creates every tested receiving consequence."""
    t = {key: uuid4() for key in ["org", "actor", "user", "vendor", "maker", "facility", "dock"]}
    t["token"] = secrets.token_urlsafe(32)
    with owner.transaction():
        owner.execute(
            "INSERT INTO fleetops.organizations(id,name) VALUES (%s,'Receiving probe')", (t["org"],)
        )
        owner.execute(
            """INSERT INTO fleetops.actors(id,org_id,type,display_name,created_by_actor_id)
            VALUES (%s,%s,'HUMAN','Receiving operator',%s)""",
            (t["actor"], t["org"], t["actor"]),
        )
        owner.execute(
            """INSERT INTO fleetops.users(id,org_id,actor_id,username,password_hash)
            VALUES (%s,%s,%s,'probe',%s)""",
            (t["user"], t["org"], t["actor"], hash_password(secrets.token_urlsafe(32))),
        )
        owner.execute(
            """INSERT INTO fleetops.sessions(id,org_id,user_id,token_digest,expires_at)
            VALUES (%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                t["org"],
                t["user"],
                token_digest(t["token"]),
                datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        for key, role in [("vendor", "VENDOR"), ("maker", "MANUFACTURER")]:
            owner.execute(
                """INSERT INTO fleetops.parties(id,org_id,display_name,created_by_actor_id)
                VALUES (%s,%s,%s,%s)""",
                (t[key], t["org"], key, t["actor"]),
            )
            owner.execute(
                """INSERT INTO fleetops.party_roles(id,org_id,party_id,role)
                VALUES (%s,%s,%s,%s)""",
                (uuid4(), t["org"], t[key], role),
            )
        owner.execute(
            """INSERT INTO fleetops.facilities(id,org_id,name,timezone,created_by_actor_id)
            VALUES (%s,%s,'Probe dock','America/Chicago',%s)""",
            (t["facility"], t["org"], t["actor"]),
        )
        owner.execute(
            """INSERT INTO fleetops.locations
            (id,org_id,facility_id,code,name,kind,created_by_actor_id)
            VALUES (%s,%s,%s,'DOCK','Receiving dock','SITE',%s)""",
            (t["dock"], t["org"], t["facility"], t["actor"]),
        )
    t["item"] = item(owner, t, serialized=True)
    t["plain"] = item(owner, t)
    return t


def item(owner, tenant, *, serialized=False, uom="EA"):
    identity = uuid4()
    owner.execute(
        """INSERT INTO fleetops.items
        (id,org_id,manufacturer_party_id,manufacturer_part_number,revision,description,
         uom,serialized,created_by_actor_id,updated_by_actor_id)
        VALUES (%s,%s,%s,%s,'A','Independent probe Item',%s,%s,%s,%s)""",
        (
            identity,
            tenant["org"],
            tenant["maker"],
            str(identity),
            uom,
            serialized,
            tenant["actor"],
            tenant["actor"],
        ),
    )
    return identity


def request(client, tenant, path, body, expected=201):
    response = client.post(path, json=body, headers={"Authorization": "Bearer " + tenant["token"]})
    assert response.status_code == expected, (path, response.status_code, response.text)
    return response.json()


def order(app, tenant, specs):
    identity, targets = uuid4(), []
    with app.transaction():
        auth(app, tenant)
        app.execute(
            """INSERT INTO fleetops.purchase_orders
            (id,org_id,vendor_party_id,po_number,created_by_actor_id,updated_by_actor_id)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (
                identity,
                tenant["org"],
                tenant["vendor"],
                str(identity),
                tenant["actor"],
                tenant["actor"],
            ),
        )
        for number, (actual_item, quantity) in enumerate(specs, 1):
            target = uuid4()
            app.execute(
                """INSERT INTO fleetops.purchase_order_lines
                (id,org_id,po_id,line_number,item_id,quantity,unit_price,
                 created_by_actor_id,updated_by_actor_id)
                VALUES (%s,%s,%s,%s,%s,%s,321.123456789,%s,%s)""",
                (
                    target,
                    tenant["org"],
                    identity,
                    number,
                    actual_item,
                    quantity,
                    tenant["actor"],
                    tenant["actor"],
                ),
            )
            targets.append(target)
        app.execute("UPDATE fleetops.purchase_orders SET status='ISSUED' WHERE id=%s", (identity,))
    return identity, targets


def line(
    tenant,
    *,
    actual_item=None,
    comparator=None,
    quantity=1,
    uom="EA",
    serialized=False,
    condition="GOOD",
    serial=None,
    unreadable=False,
    packing=None,
):
    result = {
        "item_id": str(actual_item or tenant["plain"]),
        "po_line_id": str(comparator) if comparator else None,
        "quantity": str(quantity),
        "uom": uom,
        "condition": condition,
        "packing_quantity": packing,
    }
    if serialized:
        result["unit"] = {
            "owner_party_id": str(tenant["maker"]),
            "asset_tag": str(uuid4()),
            "description": "Independent physical unit",
            "identifier": {
                "type": "MANUFACTURER_SERIAL",
                "value": None if unreadable else (serial or str(uuid4())),
                "unreadable_reason": "Unreadable mark" if unreadable else None,
            },
        }
    return result


def body(tenant, lines=(), po=None, *, reconcile=True, comparators=(), packing=None):
    return {
        "vendor_party_id": str(tenant["vendor"]),
        "dock_location_id": str(tenant["dock"]),
        "po_id": str(po) if po else None,
        "received_at": WHEN,
        "packing_reference": packing,
        "lines": list(lines),
        "comparator_ids": [str(x) for x in comparators],
        "reconcile": reconcile,
    }


def types(owner, receipt):
    return [
        row[0]
        for row in owner.execute(
            "SELECT exception_type FROM fleetops.receiving_exceptions "
            "WHERE receipt_id=%s ORDER BY exception_type",
            (receipt["id"],),
        ).fetchall()
    ]


def rows(owner, tenant, tables):
    """Read all stored columns independently, including projections and every history class."""
    return {
        name: owner.execute(
            sql.SQL("SELECT * FROM fleetops.{} WHERE org_id=%s ORDER BY 1").format(
                sql.Identifier(name)
            ),
            (tenant["org"],),
        ).fetchall()
        for name in tables
    }


def blocked(owner, pid):
    deadline = monotonic() + 10
    while monotonic() < deadline:
        if owner.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()[0]:
            return
        sleep(0.01)
    raise AssertionError("Independent contender did not reach the expected PostgreSQL lock")


def worker(database, tenant, pids, action):
    try:
        with connect(database, "fleetops_app") as connection, connection.transaction():
            auth(connection, tenant)
            connection.execute("SET LOCAL statement_timeout='15s'")
            pids.put(connection.execute("SELECT pg_backend_pid()").fetchone()[0])
            action(connection)
        return "committed"
    except psycopg.Error as error:
        return error.sqlstate


def supersede(app, tenant, po, predecessor):
    app.execute(
        """INSERT INTO fleetops.purchase_order_lines
        (id,org_id,po_id,line_number,item_id,quantity,unit_price,supersedes_line_id,
         created_by_actor_id,updated_by_actor_id) VALUES (%s,%s,%s,1,%s,1,999,%s,%s,%s)""",
        (
            uuid4(),
            tenant["org"],
            po,
            tenant["plain"],
            predecessor,
            tenant["actor"],
            tenant["actor"],
        ),
    )


def capture_sql(app, tenant, po, comparator, receipt):
    """Raw runtime receipt admission proves database locks without Python orchestration."""
    app.execute(
        """INSERT INTO fleetops.receipts(id,org_id,vendor_party_id,po_id,received_at)
        VALUES (%s,%s,%s,%s,%s)""",
        (receipt, tenant["org"], tenant["vendor"], po, WHEN),
    )
    app.execute(
        """INSERT INTO fleetops.receipt_comparators(id,org_id,receipt_id,po_line_id)
        VALUES (%s,%s,%s,%s)""",
        (uuid4(), tenant["org"], receipt, comparator),
    )
    app.execute(
        """INSERT INTO fleetops.receipt_lines
        (id,org_id,receipt_id,po_line_id,item_id,quantity,uom,condition)
        VALUES (%s,%s,%s,%s,%s,1,'EA','GOOD')""",
        (uuid4(), tenant["org"], receipt, comparator, tenant["plain"]),
    )
    app.execute(
        """INSERT INTO fleetops.receipt_reconciliations(id,org_id,receipt_id)
        VALUES (%s,%s,%s)""",
        (uuid4(), tenant["org"], receipt),
    )


def po_races(database, owner, app, tenant):
    for first in ["supersession", "receipt"]:
        po, targets = order(app, tenant, [(tenant["plain"], 1)])
        target, receipt = targets[0], uuid4()
        before = owner.execute(
            "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (target,)
        ).fetchone()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pids = Queue()
            with app.transaction():
                auth(app, tenant)
                if first == "supersession":
                    supersede(app, tenant, po, target)

                    def action(c, po=po, target=target, receipt=receipt):
                        capture_sql(c, tenant, po, target, receipt)
                else:
                    capture_sql(app, tenant, po, target, receipt)

                    def action(c, po=po, target=target):
                        supersede(c, tenant, po, target)

                future = pool.submit(worker, database, tenant, pids, action)
                blocked(owner, pids.get(timeout=10))
            outcome = future.result(timeout=20)
        assert outcome == ("40001" if first == "supersession" else "committed")
        stored = owner.execute(
            "SELECT po_line_id FROM fleetops.receipt_lines WHERE receipt_id=%s", (receipt,)
        ).fetchall()
        assert stored == ([] if first == "supersession" else [(target,)])
        assert (
            owner.execute(
                "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (target,)
            ).fetchone()
            == before
        )
        mark("C" if first == "supersession" else "D", first + "-first retained PO lock")


def parameters(tenant, receipt, serial):
    return {
        "receipt": UUID(receipt["id"]),
        "line": uuid4(),
        "item": tenant["item"],
        "asset": uuid4(),
        "tag": str(uuid4()),
        "owner": tenant["maker"],
        "identifier": uuid4(),
        "serial": serial,
        "transition": uuid4(),
    }


def normal_sql(app, tenant, values):
    app.execute(UNIT_SQL, values)
    app.execute(
        """INSERT INTO fleetops.receiving_exceptions
        (id,org_id,receipt_id,receipt_line_id,exception_type,asset_id)
        VALUES (%s,%s,%s,%s,'UNEXPECTED_ITEM',%s)""",
        (uuid4(), tenant["org"], values["receipt"], values["line"], values["asset"]),
    )


def serial_race(database, owner, app, client, tenant):
    captures = [
        request(client, tenant, "/receipts", body(tenant, reconcile=False)) for _ in range(2)
    ]
    serial = str(uuid4())
    values = [parameters(tenant, receipt, serial) for receipt in captures]
    before = rows(owner, tenant, ASSET_TABLES)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pids = Queue()
        with app.transaction():
            auth(app, tenant)
            normal_sql(app, tenant, values[0])
            future = pool.submit(
                worker, database, tenant, pids, lambda c: normal_sql(c, tenant, values[1])
            )
            blocked(owner, pids.get(timeout=10))
        assert future.result(timeout=20) == "23505"
    after = rows(owner, tenant, ASSET_TABLES)
    for table in ASSET_TABLES[:5]:
        assert len(after[table]) == len(before[table]) + 1
    for table in ASSET_TABLES[5:]:
        assert after[table] == before[table]
    assert (
        owner.execute(
            "SELECT * FROM fleetops.receipt_lines WHERE receipt_id=%s", (values[1]["receipt"],)
        ).fetchall()
        == []
    )
    assert (
        owner.execute(
            "SELECT * FROM fleetops.receiving_exceptions WHERE receipt_id=%s",
            (values[1]["receipt"],),
        ).fetchall()
        == []
    )
    mark("V", "concurrent canonical claim loses with 23505 and complete bundle rollback")
    retried = request(
        client,
        tenant,
        f"/receipts/{captures[1]['id']}/lines",
        line(tenant, actual_item=tenant["item"], serialized=True, serial=serial),
    )
    assert types(owner, retried) == ["SERIAL_MISMATCH", "UNEXPECTED_ITEM"]
    assert retried["lines"][0]["asset_id"] is None
    assert rows(owner, tenant, ASSET_TABLES) == after
    mark("W", "fresh retry records known conflict without a new Asset")
    before_duplicate = rows(owner, tenant, ASSET_TABLES + RECEIVING_TABLES)
    try:
        with app.transaction():
            auth(app, tenant)
            app.execute(UNIT_SQL, parameters(tenant, captures[1], serial))
    except psycopg.Error as error:
        assert error.sqlstate == "23505"
    else:
        raise AssertionError("Direct duplicate normal admission unexpectedly succeeded")
    assert rows(owner, tenant, ASSET_TABLES + RECEIVING_TABLES) == before_duplicate
    mark("Y", "duplicate required readable identifier cannot commit any normal unit state")


def unit_granularity_probes(owner, app, client, tenant):
    """Fresh HTTP and raw runtime SQL evidence, separate from pytest regression helpers."""
    t = tenant
    all_tables = RECEIVING_TABLES + ASSET_TABLES
    unit_sql = UNIT_SQL.replace(",1,'EA','GOOD'", ",%(quantity)s,%(uom)s,'GOOD'")
    assert unit_sql != UNIT_SQL
    empty = request(client, t, "/receipts", body(t, reconcile=False))
    for quantity, uom, code in [
        (2, "EA", "CP1"),
        (100, "CM", "CP2"),
        (2, "M", "CP3"),
        (5, "KG", "CP4"),
    ]:
        before = rows(owner, t, all_tables)
        request(
            client,
            t,
            "/receipts",
            body(t, [line(t, actual_item=t["item"], serialized=True, quantity=quantity, uom=uom)]),
            422,
        )
        assert rows(owner, t, all_tables) == before
        values = parameters(t, empty, str(uuid4())) | {
            "quantity": quantity,
            "uom": uom,
        }
        try:
            with app.transaction():
                auth(app, t)
                app.execute(unit_sql, values)
                raise AssertionError("Serialized batch passed normal runtime SQL admission")
        except psycopg.Error as error:
            assert error.sqlstate == "23514"
            assert "represents one unit" in str(error)
        assert rows(owner, t, all_tables) == before
        mark(code, f"serialized {quantity} {uom}: HTTP 422 and SQL 23514; no partial rows")

    for uom, code in [("EA", "CP5"), ("CM", "CP6"), ("KG", "CP7")]:
        before = rows(owner, t, ASSET_TABLES)
        result = request(
            client,
            t,
            "/receipts",
            body(t, [line(t, actual_item=t["item"], serialized=True, quantity=1, uom=uom)]),
        )
        stored = result["lines"][0]
        assert stored["serialized"] and stored["quantity"] == "1" and stored["uom"] == uom
        after = rows(owner, t, ASSET_TABLES)
        for table in ASSET_TABLES[:5]:
            assert len(after[table]) == len(before[table]) + 1
            key = "id" if table == "assets" else "asset_id"
            assert (
                owner.execute(
                    sql.SQL("SELECT count(*) FROM fleetops.{} WHERE {}=%s").format(
                        sql.Identifier(table), sql.Identifier(key)
                    ),
                    (stored["asset_id"],),
                ).fetchone()[0]
                == 1
            )
        assert all(after[table] == before[table] for table in ASSET_TABLES[5:])
        assert owner.execute(
            "SELECT current_state,version FROM fleetops.assets WHERE id=%s", (stored["asset_id"],)
        ).fetchone() == ("RECEIVED", 1)
        mark(code, f"serialized 1 {uom}: accepted with one complete normal Asset bundle")

    plain = item(owner, t, uom="CM")
    po, targets = order(app, t, [(plain, 100)])
    before = rows(owner, t, ASSET_TABLES)
    result = request(
        client,
        t,
        "/receipts",
        body(t, [line(t, actual_item=plain, comparator=targets[0], quantity=100, uom="CM")], po),
    )
    assert types(owner, result) == []
    stored = result["lines"][0]
    assert not stored["serialized"] and stored["quantity"] == "100" and stored["uom"] == "CM"
    assert stored["asset_id"] is None and rows(owner, t, ASSET_TABLES) == before
    mark("CP8", "nonserialized 100 CM remains valid and reconciles exactly without an Asset")

    invalid = line(t, actual_item=t["item"], serialized=True, quantity=100, uom="CM")
    before = rows(owner, t, all_tables)
    request(
        client,
        t,
        "/receipts",
        body(
            t,
            [
                line(t, actual_item=t["item"], serialized=True, condition="DAMAGED"),
                invalid,
            ],
        ),
        422,
    )
    assert rows(owner, t, all_tables) == before
    open_receipt = request(
        client,
        t,
        "/receipts",
        body(
            t,
            [
                line(t, actual_item=t["item"], serialized=True, condition="DAMAGED"),
            ],
            reconcile=False,
        ),
    )
    before = rows(owner, t, all_tables)
    request(client, t, f"/receipts/{open_receipt['id']}/lines", invalid, 422)
    assert rows(owner, t, all_tables) == before
    mark("CP9", "failed serialized batch rolls back full delivery or only failed append")

    serial = str(uuid4())
    existing = request(
        client,
        t,
        "/receipts",
        body(t, [line(t, actual_item=t["item"], serialized=True, serial=serial)]),
    )["lines"][0]["asset_id"]
    for uom in ["EA", "M", "MM", "CM", "IN", "FT", "G", "MG", "KG", "ML", "L"]:
        quantity = 100 if uom == "CM" else (5 if uom == "KG" else 2)
        before = rows(owner, t, all_tables)
        request(
            client,
            t,
            "/receipts",
            body(
                t,
                [
                    line(
                        t,
                        actual_item=t["item"],
                        serialized=True,
                        serial=serial,
                        quantity=quantity,
                        uom=uom,
                    )
                ],
            ),
            422,
        )
        assert rows(owner, t, all_tables) == before
        try:
            with app.transaction():
                auth(app, t)
                app.execute(
                    """INSERT INTO fleetops.receipt_lines
                    (id,org_id,receipt_id,item_id,quantity,uom,condition,owner_party_id,
                     observed_identifier_type,observed_identifier_value)
                    VALUES (%s,%s,%s,%s,%s,%s,'GOOD',%s,'MANUFACTURER_SERIAL',%s)""",
                    (uuid4(), t["org"], empty["id"], t["item"], quantity, uom, t["maker"], serial),
                )
                raise AssertionError("Serialized batch passed known-conflict INSERT")
        except psycopg.Error as error:
            assert error.sqlstate == "23514" and "represents one unit" in str(error)
        assert rows(owner, t, all_tables) == before
        asset_before = rows(owner, t, ASSET_TABLES)
        valid = request(
            client,
            t,
            "/receipts",
            body(t, [line(t, actual_item=t["item"], serialized=True, serial=serial, uom=uom)]),
        )
        assert types(owner, valid) == ["SERIAL_MISMATCH", "UNEXPECTED_ITEM"]
        assert valid["lines"][0]["asset_id"] is None
        mismatch = next(e for e in valid["exceptions"] if e["exception_type"] == "SERIAL_MISMATCH")
        assert mismatch["conflicting_asset_id"] == existing
        assert rows(owner, t, ASSET_TABLES) == asset_before
    mark("CP10", "known SERIAL_MISMATCH rejects batches through HTTP/SQL for all eleven UOMs")

    actual = item(owner, t, serialized=True, uom="CM")
    po, targets = order(app, t, [(actual, 2)])
    before = rows(owner, t, ASSET_TABLES)
    result = request(
        client,
        t,
        "/receipts",
        body(
            t,
            [
                line(t, actual_item=actual, comparator=targets[0], serialized=True, uom="CM"),
                line(
                    t,
                    actual_item=actual,
                    comparator=targets[0],
                    serialized=True,
                    uom="CM",
                    unreadable=True,
                ),
            ],
            po,
        ),
    )
    assert types(owner, result) == ["SERIAL_UNREADABLE"]
    assert len({observed["asset_id"] for observed in result["lines"]}) == 2
    assert all(
        observed["quantity"] == "1" and observed["uom"] == "CM" for observed in result["lines"]
    )
    after = rows(owner, t, ASSET_TABLES)
    assert all(len(after[name]) == len(before[name]) + 2 for name in ASSET_TABLES[:5])
    mark("CP11", "distinct normal 1 CM units create two bundles and retain exact local arithmetic")


def run():
    with contextmanager(disposable_database)() as database:
        with (
            connect(database, "fleetops_migrator") as owner,
            connect(database, "fleetops_app") as app,
        ):
            print("PostgreSQL:", owner.execute("SHOW server_version").fetchone()[0], flush=True)
            a, b = seed(owner), seed(owner)
            with TestClient(create_app(database.settings(a["org"]))) as client:
                unit_granularity_probes(owner, app, client, a)
                po, targets = order(app, a, [(a["plain"], 10)])
                expectation = rows(owner, a, ["purchase_orders", "purchase_order_lines"])
                results = []
                for quantity, code, wanted in [
                    (8, "E", ["SHORT"]),
                    (12, "F", ["OVER"]),
                    (10, "G", []),
                ]:
                    result = request(
                        client,
                        a,
                        "/receipts",
                        body(a, [line(a, comparator=targets[0], quantity=quantity)], po),
                    )
                    assert types(owner, result) == wanted
                    results.append(result)
                    mark(code, "receipt-local total " + str(quantity))
                assert results[0]["lines"][0]["po_line_id"] == str(targets[0])
                mark("A", "exact active comparator accepted and stored")
                r2 = request(
                    client,
                    a,
                    "/receipts",
                    body(a, [line(a, comparator=targets[0], quantity=4)], po),
                )
                assert types(owner, r2) == ["SHORT"] and types(owner, results[0]) == ["SHORT"]
                mark("H", "separate 8 and 4 receipts remain individually SHORT")
                mismatch = request(
                    client,
                    a,
                    "/receipts",
                    body(a, [line(a, comparator=targets[0], quantity=100, uom="CM")], po),
                )
                assert types(owner, mismatch) == ["UOM_MISMATCH"]
                mark("I", "raw incomparable quantity gives only UOM_MISMATCH")
                mixed = request(
                    client,
                    a,
                    "/receipts",
                    body(
                        a,
                        [
                            line(a, comparator=targets[0], quantity=10),
                            line(a, comparator=targets[0], quantity=300, uom="CM"),
                        ],
                        po,
                    ),
                )
                assert types(owner, mixed) == ["UOM_MISMATCH"]
                mark("J", "mixed UOM blocks quantity conclusion even at matching subtotal")
                substitute = item(owner, a)
                for code, condition, packing, actual, wanted in [
                    ("K", "GOOD", 2, a["plain"], ["QUANTITY_VARIANCE"]),
                    ("L", "GOOD", None, substitute, ["SUBSTITUTION"]),
                    ("M", "DAMAGED", None, a["plain"], ["DAMAGED"]),
                    ("N", "OPENED", None, a["plain"], ["OPENED"]),
                ]:
                    result = request(
                        client,
                        a,
                        "/receipts",
                        body(
                            a,
                            [
                                line(
                                    a,
                                    actual_item=actual,
                                    comparator=targets[0],
                                    quantity=10,
                                    condition=condition,
                                    packing=packing,
                                )
                            ],
                            po,
                            packing="Packing reference" if packing else None,
                        ),
                    )
                    assert types(owner, result) == wanted
                    mark(code, wanted[0] + " derived independently")
                damaged = request(client, a, "/receipts", body(a, [line(a, condition="DAMAGED")]))
                try:
                    with app.transaction():
                        auth(app, a)
                        app.execute(
                            """INSERT INTO fleetops.receiving_exceptions
                            (id,org_id,receipt_id,receipt_line_id,exception_type)
                            VALUES (%s,%s,%s,%s,'OPENED')""",
                            (uuid4(), a["org"], damaged["id"], damaged["lines"][0]["id"]),
                        )
                except psycopg.Error as error:
                    assert error.sqlstate == "23514"
                else:
                    raise AssertionError("Both conditions were admitted")
                mark("O", "database rejects DAMAGED and OPENED on one physical line")
                assert rows(owner, a, ["purchase_orders", "purchase_order_lines"]) == expectation
                mark("AD", "all receiving leaves exact PO rows, price and lineage unchanged")
                with app.transaction():
                    auth(app, a)
                    supersede(app, a, po, targets[0])
                request(client, a, "/receipts", body(a, [line(a, comparator=targets[0])], po), 409)
                mark("B", "stale target rejected without retargeting")
                po_races(database, owner, app, a)

                serial = "  independent-observed-serial / A  "
                normal = request(
                    client,
                    a,
                    "/receipts",
                    body(a, [line(a, actual_item=a["item"], serialized=True, serial=serial)]),
                )
                unit = UUID(normal["lines"][0]["asset_id"])
                bundle = owner.execute(
                    """SELECT a.version,a.current_state,a.current_assignment_id,
                    t.from_state,t.to_state,t.result_version,b.initial_owner_party_id,
                    b.initial_custodian_party_id,b.initial_location_id,a.current_location_id,
                    a.owner_party_id,a.created_by_actor_id,b.actor_id,w.actor_id,i.value
                    FROM fleetops.assets a
                    JOIN fleetops.asset_transitions t ON t.asset_id=a.id
                    JOIN fleetops.asset_initial_facts b ON b.asset_id=a.id
                    JOIN fleetops.asset_initial_assignment_facts w ON w.asset_id=a.id
                    JOIN fleetops.asset_identifiers i ON i.asset_id=a.id WHERE a.id=%s""",
                    (unit,),
                ).fetchall()
                assert bundle == [
                    (
                        1,
                        "RECEIVED",
                        None,
                        None,
                        "RECEIVED",
                        1,
                        a["maker"],
                        None,
                        a["dock"],
                        a["dock"],
                        a["maker"],
                        a["actor"],
                        a["actor"],
                        a["actor"],
                        serial,
                    )
                ]
                mark("X", "complete unique readable normal Asset bundle")
                assert a["actor"] != a["maker"] != a["vendor"]
                mark("Z", "credential Actor differs from explicit owner and vendor")
                mark("AA", "Asset and immutable baseline share the observed dock")
                unreadable = request(
                    client,
                    a,
                    "/receipts",
                    body(a, [line(a, actual_item=a["item"], serialized=True, unreadable=True)]),
                )
                assert types(owner, unreadable) == ["SERIAL_UNREADABLE", "UNEXPECTED_ITEM"]
                assert owner.execute(
                    """SELECT value,unreadable_reason FROM fleetops.asset_identifiers
                    WHERE asset_id=%s""",
                    (unreadable["lines"][0]["asset_id"],),
                ).fetchall() == [(None, "Unreadable mark")]
                mark("P", "unreadable creates normal Asset with NULL canonical value and reason")
                before = rows(owner, a, ASSET_TABLES)
                known = request(
                    client,
                    a,
                    "/receipts",
                    body(
                        a,
                        [
                            line(
                                a,
                                actual_item=a["item"],
                                serialized=True,
                                serial=serial,
                                condition="DAMAGED",
                            )
                        ],
                    ),
                )
                assert types(owner, known) == ["DAMAGED", "SERIAL_MISMATCH", "UNEXPECTED_ITEM"]
                mark(
                    "Q", "known canonical conflict derives SERIAL_MISMATCH before normal admission"
                )
                assert rows(owner, a, ASSET_TABLES) == before
                assert known["lines"][0]["asset_id"] is None
                mark(
                    "R", "known mismatch creates no Asset, canonical identifier, history or witness"
                )
                assert owner.execute(
                    """SELECT observed_identifier_type,observed_identifier_value
                    FROM fleetops.receipt_lines WHERE id=%s""",
                    (known["lines"][0]["id"],),
                ).fetchone() == ("MANUFACTURER_SERIAL", serial)
                mark("S", "observed serial type and raw value are preserved")
                assert owner.execute(
                    """SELECT conflicting_asset_id,asset_id FROM fleetops.receiving_exceptions
                    WHERE receipt_id=%s AND exception_type='SERIAL_MISMATCH'""",
                    (known["id"],),
                ).fetchone() == (unit, None)
                mark("T", "conflicting Asset is exact canonical owner, never received identity")
                assert rows(owner, a, ASSET_TABLES) == before
                mark("U", "all existing Asset facts and projections remain byte-identical")
                serial_race(database, owner, app, client, a)

                frozen = seed(owner)
                workstation = frozen["item"]
                sub, scanner, printer, network = [
                    item(owner, frozen, serialized=True) for _ in range(4)
                ]
                frozen_po, comparators = order(
                    app, frozen, [(workstation, 6), (scanner, 2), (printer, 1), (network, 1)]
                )
                frozen_before = rows(owner, frozen, ["purchase_orders", "purchase_order_lines"])
                delivery = [
                    line(
                        frozen,
                        actual_item=workstation,
                        comparator=comparators[0],
                        serialized=True,
                        unreadable=i == 0,
                    )
                    for i in range(5)
                ]
                delivery.append(
                    line(frozen, actual_item=sub, comparator=comparators[0], serialized=True)
                )
                delivery += [
                    line(frozen, actual_item=actual, comparator=target, serialized=True)
                    for actual, target in zip(
                        [scanner, printer, network], comparators[1:], strict=True
                    )
                ]
                delivery.append(line(frozen))
                result = request(client, frozen, "/receipts", body(frozen, delivery, frozen_po))
                assert Counter(types(owner, result)) == Counter(
                    {
                        "SUBSTITUTION": 1,
                        "SERIAL_UNREADABLE": 1,
                        "SHORT": 1,
                        "UNEXPECTED_ITEM": 1,
                    }
                )
                mark("AB", "frozen delivery derives exactly four governed Exceptions")
                frozen_rows = rows(owner, frozen, ASSET_TABLES)
                assert all(len(frozen_rows[name]) == 9 for name in ASSET_TABLES[:5])
                assert all(not frozen_rows[name] for name in ASSET_TABLES[5:])
                assert sum(row["asset_id"] is not None for row in result["lines"]) == 9
                assert len(result["lines"]) == 10
                assert (
                    rows(owner, frozen, ["purchase_orders", "purchase_order_lines"])
                    == frozen_before
                )
                mark("AC", "frozen delivery creates exactly nine complete Assets")
                mark("CP12", "frozen acceptance remains exactly four Exceptions and nine Assets")

                for field, value in [
                    ("vendor_party_id", b["vendor"]),
                    ("dock_location_id", b["dock"]),
                ]:
                    request(client, a, "/receipts", body(a) | {field: str(value)}, 422)
                foreign_line = line(a, actual_item=b["item"], serialized=True)
                request(client, a, "/receipts", body(a, [foreign_line]), 422)
                for field in ["owner_party_id", "custodian_party_id"]:
                    foreign_line = line(a, actual_item=a["item"], serialized=True)
                    foreign_line["unit"][field] = str(b["maker"])
                    request(client, a, "/receipts", body(a, [foreign_line]), 422)
                for table in RECEIVING_TABLES:
                    assert (
                        app.execute(
                            sql.SQL("SELECT * FROM fleetops.{}").format(sql.Identifier(table))
                        ).fetchall()
                        == []
                    )
                    with app.transaction():
                        auth(app, b)
                        assert (
                            app.execute(
                                sql.SQL("SELECT * FROM fleetops.{} WHERE org_id=%s").format(
                                    sql.Identifier(table)
                                ),
                                (a["org"],),
                            ).fetchall()
                            == []
                        )
                mark("AE", "cross-tenant references reject and every receiving table fails closed")
                for field in [
                    "conversion_factor",
                    "normalized_quantity",
                    "equivalent_quantity",
                    "canonical_uom",
                    "normalized_uom",
                    "units_per_package",
                ]:
                    request(client, a, "/receipts", body(a) | {field: 100}, 422)
                names = [
                    row[0]
                    for row in owner.execute("""
                    SELECT table_name || '.' || column_name FROM information_schema.columns
                    WHERE table_schema='fleetops'
                    UNION ALL SELECT p.proname FROM pg_proc p
                    JOIN pg_namespace n ON n.oid=p.pronamespace
                    WHERE n.nspname='fleetops'
                """).fetchall()
                ]
                assert not any(
                    any(
                        token in name
                        for token in [
                            "conversion",
                            "normalized_quantity",
                            "equivalent_quantity",
                            "canonical_uom",
                            "normalized_uom",
                            "units_per_package",
                        ]
                    )
                    for name in names
                )
                mark("AF", "no conversion inputs, fields, tables or functions")
                assert not any(
                    any(
                        token in name
                        for token in [
                            "fulfillment",
                            "remaining_quantity",
                            "open_quantity",
                            "received_to_date",
                            "supplier_completion",
                        ]
                    )
                    for name in names
                )
                mark("AG", "no cross-receipt fulfillment state")
                assert PASSED == {chr(i) for i in range(ord("A"), ord("Z") + 1)} | {
                    "AA",
                    "AB",
                    "AC",
                    "AD",
                    "AE",
                    "AF",
                    "AG",
                } | {f"CP{i}" for i in range(1, 13)}
    print(
        "PASS: all 45 independent probes (33 original + 12 corrective); "
        "disposable PostgreSQL cluster removed.",
        flush=True,
    )


if __name__ == "__main__":
    run()
