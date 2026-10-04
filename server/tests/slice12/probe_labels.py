"""Independent HTTP, raw SQL and decoded-artifact label probes on disposable PostgreSQL.

The reused helpers supply cluster isolation, fixture inputs and HTTP transport only.
No label service, schema model, renderer or pytest assertion defines an expected result.
Run with python -m server.tests.slice12.probe_labels.
"""

import hashlib
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
import pypdfium2
import zxingcpp
from fastapi.testclient import TestClient
from PIL import Image
from server.tests.conftest import disposable_database
from server.tests.slice9.probe_receiving import auth, body, connect, line, request, seed

from fleetops.api.app import create_app


def decoded(data, output_format):
    """Decode downloaded pixels, independently of the production rendering functions."""
    if output_format == "PNG":
        with Image.open(BytesIO(data)) as image:
            results = zxingcpp.read_barcodes(image)
    else:
        with pypdfium2.PdfDocument(data) as document:
            assert len(document) == 1
            page = document[0]
            bitmap = page.render(scale=300 / 72)
            try:
                results = zxingcpp.read_barcodes(bitmap.to_pil())
            finally:
                bitmap.close()
                page.close()
    assert len(results) == 1
    return results[0]


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
        # Cleanup is confined to this newly allocated FleetOps test directory.
        with TemporaryDirectory(prefix="slice12-probe-", dir=cache) as directory:
            output = Path(directory) / "artifacts"
            assert output.parent.resolve().parent == cache.resolve()
            with (
                connect(database, "fleetops_migrator") as owner,
                connect(database, "fleetops_app") as runtime,
            ):
                version = owner.execute("SHOW server_version").fetchone()[0]
                assert version.startswith("16.15"), version
                assert owner.execute(
                    "SELECT version_num FROM fleetops.alembic_version"
                ).fetchone() == ("0013_labels",)
                mark("real PostgreSQL 16.15 and the 0013 migration head")
                a, b = seed(owner), seed(owner)
                settings = replace(database.settings(a["org"]), label_output_root=output)
                with TestClient(create_app(settings)) as client:

                    def headers(tenant):
                        return {"Authorization": "Bearer " + tenant["token"]}

                    receipt = request(
                        client,
                        a,
                        "/receipts",
                        body(
                            a,
                            [
                                line(a, actual_item=a["item"], serialized=True),
                                line(a, actual_item=a["item"], serialized=True),
                                line(a, actual_item=a["plain"], quantity=20),
                            ],
                        ),
                    )
                    actual = {entry["asset_id"] for entry in receipt["lines"] if entry["asset_id"]}
                    assert len(actual) == 2
                    asset = sorted(actual)[0]
                    before = owner.execute(
                        "SELECT version,current_state FROM fleetops.assets WHERE id=%s", (asset,)
                    ).fetchone()
                    templates, jobs = {}, []
                    for symbology in ("CODE128", "DATAMATRIX"):
                        template = request(
                            client,
                            a,
                            "/label-templates",
                            dict(
                                name="Independent identity",
                                human_fields=["asset_tag", "description", "item_mpn"],
                                symbology=symbology,
                            ),
                        )
                        assert template["created_by_actor_id"] == str(a["actor"])
                        templates[symbology] = template
                        for output_format in ("PNG", "PDF"):
                            job = request(
                                client,
                                a,
                                f"/assets/{asset}/labels",
                                dict(template_id=template["id"], output_format=output_format),
                                expected=202,
                            )
                            assert job["status"] == "PENDING" and job["attempts"] == 0
                            assert job["requested_by"] == str(a["actor"])
                            assert owner.execute(
                                "SELECT status,attempts,requested_by,"
                                "render_snapshot->>'id' FROM fleetops.print_jobs WHERE id=%s",
                                (job["id"],),
                            ).fetchone() == ("PENDING", 0, a["actor"], asset)
                            if not jobs:
                                assert not output.exists()
                                mark("authenticated PENDING request commits before any output")
                            done = request(
                                client, a, f"/print-jobs/{job['id']}/dispatch", {}, expected=200
                            )
                            assert done["status"] == "SUCCEEDED" and done["attempts"] == 1
                            content = client.get(
                                f"/print-jobs/{job['id']}/content", headers=headers(a)
                            )
                            assert content.status_code == 200, content.text
                            assert (
                                hashlib.sha256(content.content).hexdigest()
                                == done["artifact_sha256"]
                            )
                            found = decoded(content.content, output_format)
                            assert found.text == asset and found.bytes == asset.encode("ascii")
                            assert found.format == (
                                zxingcpp.BarcodeFormat.Code128
                                if symbology == "CODE128"
                                else zxingcpp.BarcodeFormat.DataMatrix
                            )
                            assert (
                                request(
                                    client, a, f"/print-jobs/{job['id']}/dispatch", {}, expected=200
                                )
                                == done
                            )
                            jobs.append(done)
                            mark(f"{symbology} {output_format} decodes to only the Asset UUID")

                    rejected = client.post(
                        "/label-templates",
                        headers=headers(a),
                        json=dict(
                            name="Composite",
                            human_fields=["asset_tag"],
                            symbology="CODE128",
                            barcode_field="asset_tag",
                        ),
                    )
                    assert rejected.status_code == 422
                    try:
                        with runtime.transaction():
                            auth(runtime, a)
                            runtime.execute(
                                "INSERT INTO fleetops.label_templates "
                                "(id,org_id,name,entity_type,human_fields,symbology,barcode_field) "
                                "VALUES (%s,%s,'Composite','ASSET','[\"asset_tag\"]',"
                                "'CODE128','asset_tag')",
                                (uuid4(), a["org"]),
                            )
                    except psycopg.errors.CheckViolation:
                        pass
                    else:
                        raise AssertionError("Direct SQL accepted a nonidentity barcode field")
                    mark("API and database reject nonidentity barcode expressions")

                    for suffix, method in (("", "get"), ("/dispatch", "post"), ("/content", "get")):
                        send = getattr(client, method)
                        foreign = send(f"/print-jobs/{jobs[0]['id']}{suffix}", headers=headers(b))
                        missing = send(f"/print-jobs/{uuid4()}{suffix}", headers=headers(b))
                        assert foreign.status_code == missing.status_code == 404
                        assert foreign.json() == missing.json()
                    with runtime.transaction():
                        auth(runtime, b)
                        assert (
                            runtime.execute("SELECT id FROM fleetops.print_jobs").fetchall() == []
                        )
                        assert (
                            runtime.execute("SELECT id FROM fleetops.label_templates").fetchall()
                            == []
                        )
                    mark("foreign job operations and direct runtime RLS disclose no label records")

                    with TestClient(create_app(database.settings(a["org"]))) as unconfigured:
                        waiting = request(
                            unconfigured,
                            a,
                            f"/assets/{asset}/labels",
                            dict(template_id=templates["CODE128"]["id"]),
                            expected=202,
                        )
                        failed = request(
                            unconfigured,
                            a,
                            f"/print-jobs/{waiting['id']}/dispatch",
                            {},
                            expected=503,
                        )
                        assert failed["status"] == "FAILED" and failed["attempts"] == 1
                        assert owner.execute(
                            "SELECT status,attempts,error_code "
                            "FROM fleetops.print_jobs WHERE id=%s",
                            (waiting["id"],),
                        ).fetchone() == ("FAILED", 1, "OUTPUT_UNAVAILABLE")
                    retry = request(
                        client, a, f"/print-jobs/{waiting['id']}/dispatch", {}, expected=200
                    )
                    assert retry["status"] == "SUCCEEDED" and retry["attempts"] == 2
                    mark("output failure is durably recorded and a configured retry succeeds")

                    batch = request(
                        client,
                        a,
                        f"/receipts/{receipt['id']}/labels",
                        dict(template_id=templates["DATAMATRIX"]["id"]),
                        expected=202,
                    )
                    assert len(batch) == 2 and {job["entity_id"] for job in batch} == actual
                    mark("receipt batch targets exactly two actual Assets, ignoring 20 loose units")

                    original = jobs[0]
                    path = output / str(a["org"]) / (original["id"] + ".png")
                    path.write_bytes(b"privileged corruption")
                    broken = client.get(f"/print-jobs/{original['id']}/content", headers=headers(a))
                    assert (
                        broken.status_code == 503 and b"privileged corruption" not in broken.content
                    )
                    assert str(output) not in broken.text
                    assert (
                        client.get(f"/print-jobs/{original['id']}", headers=headers(a)).json()
                        == original
                    )
                    assert (
                        owner.execute(
                            "SELECT version,current_state FROM fleetops.assets WHERE id=%s",
                            (asset,),
                        ).fetchone()
                        == before
                    )
                    mark("corrupt output fails closed; Asset state/version remain unchanged")

                    refused = database.migrate("downgrade", "0012_evidence")
                    assert refused.returncode != 0
                    assert "Cannot downgrade accepted label records" in refused.stderr
                    assert owner.execute(
                        "SELECT version_num FROM fleetops.alembic_version"
                    ).fetchone() == ("0013_labels",)
                    assert (
                        owner.execute("SELECT count(*) FROM fleetops.print_jobs").fetchone()[0] == 7
                    )
                    mark("downgrade refuses accepted label records atomically")
                    assert len(passed) == 12
    finally:
        cluster.close()
    print("12/12 independent label probes PASS; disposable resources removed.", flush=True)


if __name__ == "__main__":
    main()
