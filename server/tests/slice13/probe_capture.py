"""Independent capture HTTP/raw-psycopg proofs on a disposable PostgreSQL cluster.

Only cluster isolation, declared fixture inputs and HTTP transport are reused.
No capture service, schema mapping or pytest assertion defines the oracle.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from server.tests.conftest import disposable_database
from server.tests.slice9.probe_receiving import auth, body, connect, line, request, seed

from fleetops.api.app import create_app


def main():
    cluster = disposable_database()
    database = next(cluster)
    passed = []

    def mark(description):
        passed.append(description)
        print(f"PASS {len(passed):02}: {description}", flush=True)

    try:
        cache = Path(__file__).resolve().parents[3] / ".pytest_cache"
        cache.mkdir(exist_ok=True)
        with TemporaryDirectory(prefix="slice13-probe-", dir=cache) as directory:
            with (
                connect(database, "fleetops_migrator") as owner,
                connect(database, "fleetops_app") as runtime,
            ):
                assert owner.execute("SHOW server_version").fetchone()[0].startswith("16.15")
                assert owner.execute(
                    "SELECT version_num FROM fleetops.alembic_version"
                ).fetchone() == ("0014_capture",)
                mark("real PostgreSQL 16.15 and current capture migration")
                a, b = seed(owner), seed(owner)
                settings = replace(
                    database.settings(a["org"]), evidence_root=Path(directory) / "evidence"
                )
                with TestClient(create_app(settings)) as client:

                    def headers(tenant):
                        return {"Authorization": "Bearer " + tenant["token"]}

                    receipt = request(
                        client,
                        a,
                        "/receipts",
                        body(a, lines=[line(a, actual_item=a["item"], serialized=True)]),
                    )
                    asset = receipt["lines"][0]["asset_id"]

                    def op(**changes):
                        return {
                            "operation_id": str(uuid4()),
                            "actor_id": str(a["actor"]),
                            "client_id": str(uuid4()),
                            "client_epoch": str(uuid4()),
                            "client_seq": 1,
                            "entity_type": "ASSET",
                            "entity_id": asset,
                            "expected_version": 1,
                            "operation": "MOVE",
                            "payload": {"to_location_id": None, "reason": "Observed move"},
                            "occurred_at": "1970-01-01T00:00:00Z",
                            **changes,
                        }

                    def submit(operations, tenant=a):
                        return request(
                            client,
                            tenant,
                            "/capture/operations",
                            {"operations": operations},
                            expected=200,
                        )

                    resolved = client.get("/resolve/" + asset, headers=headers(a))
                    assert resolved.status_code == 200 and resolved.json()["entity_type"] == "ASSET"
                    assert client.get("/resolve/" + asset, headers=headers(b)).status_code == 404
                    session = owner.execute(
                        "SELECT id FROM fleetops.sessions WHERE org_id=%s", (a["org"],)
                    ).fetchone()[0]
                    assert (
                        client.get("/resolve/" + str(session), headers=headers(a)).status_code
                        == 404
                    )
                    mark("opaque UUID resolution is scoped and hides credentials")

                    queued = op()
                    replies = [submit([queued])[0] for _ in range(3)]
                    assert [r["sync_state"] for r in replies] == [
                        "APPLIED",
                        "DUPLICATE",
                        "DUPLICATE",
                    ]
                    assert replies[0]["result"] == replies[1]["result"] == replies[2]["result"]
                    assert owner.execute(
                        "SELECT count(*) FROM fleetops.asset_movements WHERE asset_id=%s", (asset,)
                    ).fetchone() == (1,)
                    row = owner.execute(
                        "SELECT actor_id,occurred_at,recorded_at FROM fleetops.capture_operations "
                        "WHERE operation_id=%s",
                        (queued["operation_id"],),
                    ).fetchone()
                    assert row[0] == a["actor"] and row[1].year == 1970 and row[2].year >= 2026
                    mark("three submissions: one effect, original results, distinct clock facts")

                    stream, epoch = str(uuid4()), str(uuid4())
                    first = op(
                        client_id=stream, client_epoch=epoch, client_seq=1, expected_version=2
                    )
                    second = op(
                        client_id=stream, client_epoch=epoch, client_seq=2, expected_version=3
                    )
                    results = submit([second, first])
                    assert results[0]["result"]["result_version"] == 4
                    assert results[1]["result"]["result_version"] == 3
                    mark("shuffled batches preserve sequence order and response positions")

                    gap = op(client_id=stream, client_epoch=epoch, client_seq=4, expected_version=4)
                    assert submit([gap])[0]["sequence_flags"] == ["SEQUENCE_GAP"]
                    reused = op(
                        client_id=stream, client_epoch=epoch, client_seq=4, expected_version=5
                    )
                    assert submit([reused])[0]["sequence_flags"] == ["SEQUENCE_REUSED"]
                    reset = op(client_id=stream, client_seq=1, expected_version=6)
                    assert submit([reset])[0]["sequence_flags"] == []
                    mark("gaps and different-ID reuse are flagged; new epochs reset independently")

                    snapshot = owner.execute(
                        "SELECT version,current_location_id,current_state FROM fleetops.assets "
                        "WHERE id=%s",
                        (asset,),
                    ).fetchone()
                    stale = submit([op(expected_version=1)])[0]
                    assert (
                        stale["sync_state"] == "REJECTED"
                        and stale["result"]["code"] == "SYNC_CONFLICT"
                    )
                    conflict = stale["result"]["exception_id"]
                    assert stale["result"]["expected"]["version"] == 1
                    assert stale["result"]["expected"]["location_id"] == str(a["dock"])
                    assert stale["result"]["current"]["version"] == 7
                    assert (
                        owner.execute(
                            "SELECT version,current_location_id,current_state FROM fleetops.assets "
                            "WHERE id=%s",
                            (asset,),
                        ).fetchone()
                        == snapshot
                    )
                    mark("stale capture: truthful conflict, no domain changes")

                    resolution = op(
                        operation="RESOLVE",
                        entity_type="EXCEPTION",
                        entity_id=conflict,
                        expected_version=2147483647,
                        payload={"expected_status": "OPEN", "note": "Checked floor"},
                    )
                    assert submit([resolution])[0]["sync_state"] == "APPLIED"
                    assert submit([resolution])[0]["sync_state"] == "DUPLICATE"
                    logical = client.get("/exceptions/" + conflict, headers=headers(a)).json()
                    assert logical["status"] == "RESOLVED" and len(logical["events"]) == 1
                    assert logical["resolved_by_actor_id"] == str(a["actor"])
                    with runtime.transaction():
                        auth(runtime, a)
                        try:
                            with runtime.transaction():
                                runtime.execute("DELETE FROM fleetops.sync_conflict_events")
                            raise AssertionError("Mutable sync history")
                        except psycopg.errors.InsufficientPrivilege:
                            pass
                    mark("resolution retains authenticated immutable history")

                    opened = request(client, a, "/receipts", body(a, lines=[], reconcile=False))
                    receive = op(
                        operation="RECEIVE_SCAN",
                        entity_type="RECEIPT",
                        entity_id=opened["id"],
                        expected_version=None,
                        payload={
                            "line": line(a, actual_item=a["plain"], quantity=2),
                            "reconcile": True,
                        },
                    )
                    first_receive = submit([receive])[0]
                    assert first_receive["sync_state"] == "APPLIED"
                    assert submit([receive])[0]["result"] == first_receive["result"]
                    assert owner.execute(
                        "SELECT count(*) FROM fleetops.receipt_lines WHERE receipt_id=%s",
                        (opened["id"],),
                    ).fetchone() == (1,)
                    mark("receive scan reuses receipt-local reality and reconciles once")

                    uploaded = client.post(
                        "/attachments",
                        headers=headers(a),
                        params={
                            "source_type": "PHOTO",
                            "captured_at": "1970-01-01T00:00:00Z",
                            "original_filename": "scan.png",
                            "media_type": "image/png",
                        },
                        content=b"Independent image",
                    )
                    assert uploaded.status_code == 201, uploaded.text
                    attachment = uploaded.json()["id"]
                    link = op(
                        operation="ATTACH_EVIDENCE",
                        expected_version=2147483647,
                        payload={"attachment_id": attachment, "link_role": "OTHER"},
                    )
                    assert submit([link])[0]["sync_state"] == "APPLIED"
                    assert submit([link])[0]["sync_state"] == "DUPLICATE"
                    assert owner.execute(
                        "SELECT count(*) FROM fleetops.attachment_links WHERE attachment_id=%s",
                        (attachment,),
                    ).fetchone() == (1,)
                    assert owner.execute(
                        "SELECT version FROM fleetops.assets WHERE id=%s", (asset,)
                    ).fetchone() == (7,)
                    mark("verified evidence links apply independently of Asset version")

                    forged = op(actor_id=str(b["actor"]), expected_version=7)
                    assert submit([forged])[0]["sync_state"] == "REJECTED"
                    stolen = dict(queued, actor_id=str(b["actor"]), entity_id=str(uuid4()))
                    collision = submit([stolen], b)[0]
                    assert (
                        collision["result"] == {"code": "OPERATION_UNAVAILABLE"}
                        and collision["recorded_at"] is None
                    )
                    mark("Actor claims and global operation-ID collisions cannot borrow authority")

                    raced = op(expected_version=7)
                    with runtime.transaction():
                        auth(runtime, a)
                        runtime.execute(
                            "SELECT id FROM fleetops.assets WHERE id=%s FOR UPDATE", (asset,)
                        )
                        blocker = runtime.execute("SELECT pg_backend_pid()").fetchone()[0]
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            futures = [executor.submit(submit, [raced]) for _ in range(2)]
                            try:
                                deadline = monotonic() + 15
                                while (
                                    owner.execute(
                                        "SELECT count(*) FROM pg_locks WHERE NOT granted "
                                        "AND locktype='advisory'"
                                    ).fetchone()[0]
                                    < 1
                                ):
                                    assert monotonic() < deadline, "No duplicate advisory waiter"
                                    sleep(0.01)
                                assert owner.execute(
                                    "SELECT EXISTS(SELECT 1 FROM pg_locks l WHERE NOT granted "
                                    "AND %s=ANY(pg_blocking_pids(l.pid)))",
                                    (blocker,),
                                ).fetchone() == (True,)
                                runtime.execute(
                                    "SELECT * FROM fleetops.transition_asset("
                                    "%s,7,'RECEIVED','IN_STOCK',"
                                    "'Online winner','2026-10-04T12:00:00Z',NULL,NULL,%s)",
                                    (asset, uuid4()),
                                )
                                runtime.execute("COMMIT")
                                raced_results = [future.result(timeout=15)[0] for future in futures]
                            finally:
                                runtime.execute("ROLLBACK")
                    assert sorted(r["sync_state"] for r in raced_results) == [
                        "DUPLICATE",
                        "REJECTED",
                    ]
                    assert raced_results[0]["result"] == raced_results[1]["result"]
                    assert owner.execute(
                        "SELECT count(*) FROM fleetops.capture_operations WHERE operation_id=%s",
                        (raced["operation_id"],),
                    ).fetchone() == (1,)
                    assert owner.execute(
                        "SELECT count(*) FROM fleetops.sync_conflicts WHERE operation_id=%s",
                        (raced["operation_id"],),
                    ).fetchone() == (1,)
                    mark("observed DB waiters prove duplicate and cross-class race serialization")

                    assert client.post("/auth/logout", headers=headers(a)).status_code == 204
                    assert (
                        client.post(
                            "/capture/operations", headers=headers(a), json={"operations": [queued]}
                        ).status_code
                        == 401
                    )
                    refused = database.migrate("downgrade", "0013_labels")
                    assert (
                        refused.returncode != 0
                        and "Cannot downgrade accepted capture records" in refused.stderr
                    )
                    assert owner.execute(
                        "SELECT version_num FROM fleetops.alembic_version"
                    ).fetchone() == ("0014_capture",)
                    mark("revoked credentials cannot replay; downgrade preserves accepted captures")
                    assert len(passed) == 12
    finally:
        cluster.close()
    print("12/12 independent capture probes PASS; disposable resources removed.", flush=True)


if __name__ == "__main__":
    main()
