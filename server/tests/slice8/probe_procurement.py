"""Independent psycopg SQL probes; run with python -m server.tests.slice8.probe_procurement.

Only the disposable PostgreSQL/bootstrap harness is reused. Assertions neither call
the procurement service nor import its SQLAlchemy tables or pytest test functions.
The cluster is destroyed on success or failure, including all administrative seed data.
"""

import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from queue import Queue
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from psycopg import sql
from server.tests.conftest import disposable_database

from fleetops.auth import hash_password, token_digest


def connect(database, role):
    """Separate real login domains, never SET ROLE or an elevated application connection."""
    url = database.url(role)
    return psycopg.connect(
        host=url.host,
        port=url.port,
        dbname=url.database,
        user=url.username,
        password=url.password,
        autocommit=True,
    )


def authenticate(connection, seed):
    """Carry the opaque credential rather than Actor testimony into each transaction."""
    connection.execute("SELECT set_config('fleetops.org_id', %s, true)", (str(seed["org"]),))
    connection.execute(
        "SELECT set_config('fleetops.session_digest_hex', %s, true)", (seed["digest"].hex(),)
    )


def seed_tenant(connection):
    """Administrative inputs are explicit and independent from the asserted runtime DML."""
    seed = {name: uuid4() for name in ("org", "actor", "user", "vendor", "maker", "item")}
    seed["digest"] = token_digest(secrets.token_urlsafe(32))
    with connection.transaction():
        connection.execute(
            "INSERT INTO fleetops.organizations(id,name) VALUES (%s,%s)",
            (seed["org"], "Independent procurement probe"),
        )
        connection.execute(
            """INSERT INTO fleetops.actors
            (id,org_id,type,display_name,created_by_actor_id) VALUES (%s,%s,'HUMAN',%s,%s)""",
            (seed["actor"], seed["org"], "Operator", seed["actor"]),
        )
        connection.execute(
            """INSERT INTO fleetops.users
            (id,org_id,actor_id,username,password_hash) VALUES (%s,%s,%s,%s,%s)""",
            (
                seed["user"],
                seed["org"],
                seed["actor"],
                "probe",
                hash_password(secrets.token_urlsafe(32)),
            ),
        )
        connection.execute(
            """INSERT INTO fleetops.sessions
            (id,org_id,user_id,token_digest,expires_at) VALUES (%s,%s,%s,%s,%s)""",
            (
                uuid4(),
                seed["org"],
                seed["user"],
                seed["digest"],
                datetime.now(UTC) + timedelta(hours=1),
            ),
        )
        for party, role in [("vendor", "VENDOR"), ("maker", "MANUFACTURER")]:
            connection.execute(
                """INSERT INTO fleetops.parties
                (id,org_id,display_name,created_by_actor_id) VALUES (%s,%s,%s,%s)""",
                (seed[party], seed["org"], role, seed["actor"]),
            )
            connection.execute(
                """INSERT INTO fleetops.party_roles
                (id,org_id,party_id,role) VALUES (%s,%s,%s,%s)""",
                (uuid4(), seed["org"], seed[party], role),
            )
        connection.execute(
            """INSERT INTO fleetops.items
            (id,org_id,manufacturer_party_id,manufacturer_part_number,revision,description,
             uom,serialized,created_by_actor_id,updated_by_actor_id)
            VALUES (%s,%s,%s,'RAW-PROBE','A','Raw probe','EA',false,%s,%s)""",
            (seed["item"], seed["org"], seed["maker"], seed["actor"], seed["actor"]),
        )
    return seed


INSERT_LINE = """INSERT INTO fleetops.purchase_order_lines
    (id,org_id,po_id,line_number,item_id,quantity,unit_price,supersedes_line_id,
     created_by_actor_id,updated_by_actor_id)
    VALUES (%s,%s,%s,%s,%s,2.125,0.123456789123,%s,%s,%s) RETURNING id,uom"""


def line_params(seed, po, number, predecessor=None):
    return (
        uuid4(),
        seed["org"],
        po,
        number,
        seed["item"],
        predecessor,
        seed["actor"],
        seed["actor"],
    )


def contender(database, seed, params, pids):
    """Report success only after COMMIT; return only SQLSTATE for the losing contender."""
    try:
        with connect(database, "fleetops_app") as connection:
            with connection.transaction():
                authenticate(connection, seed)
                connection.execute("SET LOCAL statement_timeout='15s'")
                pids.put(connection.execute("SELECT pg_backend_pid()").fetchone()[0])
                row = connection.execute(INSERT_LINE, params).fetchone()
        return "success", row
    except psycopg.Error as error:
        return error.sqlstate, None


def race(database, owner, seed, po, number, predecessor=None):
    """Hold the parent lock until both independent SQL commands are observed blocked."""
    pids = Queue()
    with ThreadPoolExecutor(max_workers=2) as pool:
        with owner.transaction():
            owner.execute(
                "SELECT id FROM fleetops.purchase_orders WHERE id=%s FOR NO KEY UPDATE", (po,)
            )
            futures = [
                pool.submit(
                    contender, database, seed, line_params(seed, po, number, predecessor), pids
                )
                for _ in range(2)
            ]
            waiting = [pids.get(timeout=10) for _ in futures]
            deadline = monotonic() + 10
            while True:
                if all(
                    owner.execute(
                        "SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)
                    ).fetchone()[0]
                    for pid in waiting
                ):
                    break
                assert monotonic() < deadline, "Expected independent raw SQL lock contention"
                sleep(0.01)
        outcomes = [future.result(timeout=20) for future in futures]
    assert sum(status == "success" for status, _ in outcomes) == 1
    assert sorted(status for status, _ in outcomes) == [
        "23505" if predecessor is None else "40001",
        "success",
    ]
    return next(row for status, row in outcomes if status == "success")


def run():
    """Probe durable invariants on a separately provisioned PostgreSQL 16 cluster."""
    with contextmanager(disposable_database)() as database:
        with (
            connect(database, "fleetops_migrator") as owner,
            connect(database, "fleetops_app") as app,
        ):
            print("PostgreSQL:", owner.execute("SHOW server_version").fetchone()[0])
            seed, po = seed_tenant(owner), uuid4()
            with app.transaction():
                authenticate(app, seed)
                app.execute(
                    """INSERT INTO fleetops.purchase_orders
                    (id,org_id,vendor_party_id,po_number,created_by_actor_id,updated_by_actor_id)
                    VALUES (%s,%s,%s,'RAW-PO',%s,%s)""",
                    (po, seed["org"], seed["vendor"], seed["actor"], seed["actor"]),
                )
            first_id, first_uom = race(database, owner, seed, po, 1)
            assert first_uom == "EA"
            with app.transaction():
                authenticate(app, seed)
                app.execute(
                    "UPDATE fleetops.purchase_orders SET status='ISSUED' WHERE id=%s", (po,)
                )
            before = owner.execute(
                "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (first_id,)
            ).fetchone()
            with owner.transaction():
                owner.execute("UPDATE fleetops.items SET uom='M' WHERE id=%s", (seed["item"],))
            second_id, second_uom = race(database, owner, seed, po, 1, first_id)
            assert second_uom == "M"
            assert (
                owner.execute(
                    "SELECT * FROM fleetops.purchase_order_lines WHERE id=%s", (first_id,)
                ).fetchone()
                == before
            )
            with app.transaction():
                authenticate(app, seed)
                active = app.execute(
                    """SELECT l.id FROM fleetops.purchase_order_lines l
                    WHERE l.po_id=%s AND NOT EXISTS (SELECT 1 FROM fleetops.purchase_order_lines s
                        WHERE s.org_id=l.org_id AND s.po_id=l.po_id
                          AND s.supersedes_line_id=l.id)""",
                    (po,),
                ).fetchall()
                assert active == [(second_id,)]
            for table, identity in [
                ("purchase_orders", po),
                ("purchase_order_lines", first_id),
                ("purchase_order_lines", second_id),
            ]:
                try:
                    with owner.transaction():
                        owner.execute(
                            sql.SQL(
                                "UPDATE fleetops.{} SET updated_at=updated_at WHERE id=%s"
                            ).format(sql.Identifier(table)),
                            (identity,),
                        )
                except psycopg.Error as error:
                    assert error.sqlstate == "23514"
                else:
                    raise AssertionError("Ordinary migrator write bypassed issued freeze")
            print("PASS: raw SQL duplicate-root race, supersession race, one active leaf,")
            print("      EA -> M snapshots, predecessor unchanged, owner freeze on all versions.")
    print("PASS: independent disposable cluster removed.")


if __name__ == "__main__":
    run()
