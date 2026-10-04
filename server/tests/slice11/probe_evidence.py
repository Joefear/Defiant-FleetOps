"""Independent HTTP/filesystem/raw-psycopg evidence oracles on disposable PostgreSQL.

Run with python -m server.tests.slice11.probe_evidence. Existing helpers supply only
cluster isolation, fixture inputs and request transport; no evidence service, schema
model or pytest test assertion supplies the expected outcomes.
"""

import hashlib
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql
from server.tests.conftest import disposable_database
from server.tests.slice9.probe_receiving import (
    WHEN,
    auth,
    body,
    connect,
    line,
    request,
    seed,
)

from fleetops.api.app import create_app


def main():
    """Exercise the production routes and inspect independently persisted facts."""
    cluster = disposable_database()
    database = next(cluster)
    passed = []

    def mark(description):
        passed.append(description)
        print(f"PASS {len(passed):02}: {description}", flush=True)

    try:
        cache = Path(__file__).resolve().parents[3] / ".pytest_cache"
        cache.mkdir(exist_ok=True)
        # TemporaryDirectory removes only its newly allocated directory, under this
        # verified FleetOps cache. It never selects deployment evidence for cleanup.
        with TemporaryDirectory(prefix="slice11-probe-", dir=cache) as directory:
            root = Path(directory)
            assert root.resolve().parent == cache.resolve()
            with (
                connect(database, "fleetops_migrator") as owner,
                connect(database, "fleetops_app") as app,
            ):
                version = owner.execute("SHOW server_version").fetchone()[0]
                assert version.startswith("16.15"), version
                print("PostgreSQL " + version, flush=True)
                a, b = seed(owner), seed(owner)
                settings = replace(database.settings(a["org"]), evidence_root=root)
                with TestClient(create_app(settings)) as client:

                    def headers(tenant):
                        return {"Authorization": "Bearer " + tenant["token"]}

                    def upload(tenant, data, **changes):
                        return client.post(
                            "/attachments",
                            headers=headers(tenant),
                            content=data,
                            params=dict(
                                source_type="DOCUMENT",
                                captured_at=WHEN,
                                media_type="text/plain",
                                original_filename="probe.txt",
                                **changes,
                            ),
                        )

                    received = request(
                        client,
                        a,
                        "/receipts",
                        body(
                            a,
                            [
                                line(a, actual_item=a["item"], serialized=True),
                            ],
                        ),
                    )
                    asset = received["lines"][0]["asset_id"]
                    response = upload(a, b"disposal evidence")
                    assert response.status_code == 201, response.text
                    capture = response.json()
                    assert capture["captured_by"] == str(a["actor"])
                    digest = hashlib.sha256(b"disposal evidence").hexdigest()
                    assert capture["sha256"] == digest
                    assert (root / str(a["org"]) / digest).read_bytes() == b"disposal evidence"
                    mark("server hash, stored bytes and authenticated capture agree")

                    duplicate = upload(a, b"disposal evidence")
                    assert duplicate.json() == capture
                    assert (
                        owner.execute(
                            "SELECT count(*) FROM fleetops.attachments WHERE org_id=%s", (a["org"],)
                        ).fetchone()[0]
                        == 1
                    )
                    mark("duplicate bytes reuse immutable first-capture identity")

                    assert upload(a, b"bad hash", sha256="0" * 64).status_code == 422
                    assert (
                        owner.execute("SELECT count(*) FROM fleetops.attachments").fetchone()[0]
                        == 1
                    )
                    mark("hash mismatch creates no attachment")

                    foreign = upload(b, b"disposal evidence")
                    assert foreign.status_code == 201 and foreign.json()["id"] != capture["id"]
                    assert (
                        client.get(
                            f"/attachments/{capture['id']}/content", headers=headers(b)
                        ).status_code
                        == 404
                    )
                    mark(
                        "tenant hash deduplication never exposes another tenant's identity or bytes"
                    )

                    newer = upload(a, b"amended record", supersedes_attachment_id=capture["id"])
                    assert newer.status_code == 201
                    assert newer.json()["supersedes_attachment_id"] == capture["id"]
                    old = client.get(f"/attachments/{capture['id']}/content", headers=headers(a))
                    assert old.status_code == 200 and old.content == b"disposal evidence"
                    mark("supersession retains original bytes and provenance")

                    linked = request(
                        client,
                        a,
                        f"/attachments/{capture['id']}/links",
                        dict(entity_type="ASSET", entity_id=asset, link_role="DISPOSAL_EVIDENCE"),
                    )
                    assert linked["actor_id"] == str(a["actor"])
                    request(
                        client,
                        a,
                        f"/attachments/{capture['id']}/links",
                        dict(
                            entity_type="ASSET",
                            entity_id=str(uuid4()),
                            link_role="DISPOSAL_EVIDENCE",
                        ),
                        expected=422,
                    )
                    mark("typed evidence links validate target and authenticated linker")

                    for table in ("attachments", "attachment_links"):
                        assert (
                            app.execute(
                                sql.SQL("SELECT count(*) FROM fleetops.{}").format(
                                    sql.Identifier(table)
                                )
                            ).fetchone()[0]
                            == 0
                        )
                        with app.transaction():
                            auth(app, b)
                            assert (
                                app.execute(
                                    sql.SQL(
                                        "SELECT count(*) FROM fleetops.{} WHERE org_id=%s"
                                    ).format(sql.Identifier(table)),
                                    (a["org"],),
                                ).fetchone()[0]
                                == 0
                            )
                    mark("populated evidence tables enforce missing-context and cross-tenant RLS")

                    for table in ("attachments", "attachment_links"):
                        for statement in ("UPDATE {} SET id=id", "DELETE FROM {}", "TRUNCATE {}"):
                            try:
                                with app.transaction():
                                    auth(app, a)
                                    app.execute(
                                        sql.SQL(statement).format(sql.Identifier("fleetops", table))
                                    )
                            except psycopg.Error as error:
                                assert error.sqlstate == "42501"
                            else:
                                raise AssertionError("Runtime mutated immutable evidence")
                    mark("runtime UPDATE DELETE TRUNCATE denied on both evidence tables")

                    try:
                        with app.transaction():
                            app.execute(
                                "SELECT set_config('fleetops.org_id',%s,true)", (str(a["org"]),)
                            )
                            app.execute(
                                """
                              INSERT INTO fleetops.attachments
                              (id,org_id,sha256,byte_size,media_type,storage_key,source_type,
                               captured_at,original_filename)
                              VALUES (%s,%s,%s,0,'text/plain',%s,'OTHER',now(),'no-credential')
                            """,
                                (uuid4(), a["org"], "a" * 64, f"{a['org']}/{'a' * 64}"),
                            )
                    except psycopg.Error as error:
                        assert error.sqlstate == "42501"
                    else:
                        raise AssertionError("Tenant scope alone supplied capture authority")
                    mark("organization context alone cannot authenticate capture")

                    request(
                        client,
                        a,
                        f"/attachments/{capture['id']}/links",
                        dict(entity_type="ASSET", entity_id=asset, link_role="CONFIG_EVIDENCE"),
                    )
                    config = request(
                        client,
                        a,
                        f"/assets/{asset}/configurations",
                        dict(
                            image_name="probe",
                            image_version="1",
                            config_profile="floor",
                            applied_at=WHEN,
                            evidence_ref=capture["id"],
                        ),
                    )
                    assert config["evidence_ref"] == capture["id"]
                    assert (
                        owner.execute(
                            "SELECT version FROM fleetops.assets WHERE id=%s", (asset,)
                        ).fetchone()[0]
                        == 1
                    )
                    mark(
                        "configuration accepts verified evidence without producing an Asset version"
                    )

                    request(
                        client,
                        a,
                        f"/assets/{asset}/transitions",
                        dict(
                            expected_version=1,
                            from_state="RECEIVED",
                            to_state="IN_STOCK",
                            reason="Store received unit",
                            occurred_at=WHEN,
                        ),
                    )
                    request(
                        client,
                        a,
                        f"/assets/{asset}/transitions",
                        dict(
                            expected_version=2,
                            from_state="IN_STOCK",
                            to_state="RETIRED",
                            reason="Dispose received unit",
                            occurred_at=WHEN,
                            evidence_ref=capture["id"],
                        ),
                    )
                    assert owner.execute(
                        "SELECT current_state,version FROM fleetops.assets WHERE id=%s", (asset,)
                    ).fetchone() == ("RETIRED", 3)
                    mark("verified disposal evidence admits the legal retirement with one version")

                    (root / str(a["org"]) / digest).write_bytes(b"privileged storage corruption")
                    assert (
                        client.get(
                            f"/attachments/{capture['id']}/content", headers=headers(a)
                        ).status_code
                        == 503
                    )
                    mark("retrieval detects changed bytes rather than returning false evidence")
                    assert len(passed) == 12
    finally:
        cluster.close()
    print("12/12 independent evidence probes PASS; disposable resources removed.", flush=True)


if __name__ == "__main__":
    main()
