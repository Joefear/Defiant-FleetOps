"""Capture-time truth, tenant privacy and immutable supersession through the real API."""

import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from server.tests.auth_context import set_authenticated
from server.tests.slice11.conftest import headers, link, transition, upload
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.api.app import create_app
from fleetops.db.metadata import attachment_links, attachments


def test_capture_dedup_supersession_and_historical_retrieval(
    evidence_client, asset_data, evidence_root, migrator_connection
):
    a, b = asset_data
    first = upload(evidence_client, a, b"original", sha256=hashlib.sha256(b"original").hexdigest())
    assert first.status_code == 201, first.text
    row = first.json()
    assert row["captured_by"] == str(a.actor_id)
    assert row["captured_at"] == "2026-01-02T03:04:05Z"
    assert row["byte_size"] == 8
    assert row["recorded_at"] != row["captured_at"]
    again = upload(evidence_client, a, b"original", original_filename="renamed.txt")
    assert again.status_code == 201
    assert again.json() == row
    newer = upload(evidence_client, a, b"modified", supersedes_attachment_id=row["id"])
    assert newer.status_code == 201, newer.text
    assert newer.json()["id"] != row["id"]
    assert newer.json()["supersedes_attachment_id"] == row["id"]
    for captured, content in ((row, b"original"), (newer.json(), b"modified")):
        response = evidence_client.get(f"/attachments/{captured['id']}/content", headers=headers(a))
        assert response.status_code == 200 and response.content == content
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert (evidence_root / str(a.org_id) / captured["sha256"]).read_bytes() == content
    other = upload(evidence_client, b, b"original")
    assert other.status_code == 201 and other.json()["id"] != row["id"]
    assert other.json()["captured_by"] == str(b.actor_id)
    assert len(migrator_connection.execute(select(attachments)).all()) == 3


@pytest.mark.parametrize("suffix", ["", "/content", "/links"])
def test_foreign_tenant_reads_are_indistinguishable_from_missing(
    evidence_client, asset_data, suffix
):
    a, b = asset_data
    row = upload(evidence_client, a).json()
    foreign = evidence_client.get(f"/attachments/{row['id']}{suffix}", headers=headers(b))
    missing = evidence_client.get(f"/attachments/{uuid7()}{suffix}", headers=headers(b))
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


@pytest.mark.parametrize("forbidden", ["org_id", "captured_by", "recorded_at", "storage_key", "id"])
def test_upload_rejects_caller_authority(evidence_client, asset_data, forbidden, evidence_root):
    response = upload(evidence_client, asset_data[0], **{forbidden: "forged"})
    assert response.status_code == 422
    assert any(e["type"] == "extra_forbidden" for e in response.json()["detail"])
    assert not evidence_root.exists()


def test_hash_mismatch_and_unauthenticated_upload_write_nothing(
    evidence_client, asset_data, evidence_root, migrator_connection
):
    response = upload(evidence_client, asset_data[0], sha256="0" * 64)
    assert response.status_code == 422
    response = evidence_client.post("/attachments", content=b"anything")
    assert response.status_code == 401
    assert not evidence_root.exists()
    assert migrator_connection.execute(select(attachments)).all() == []


def test_supersession_rejects_foreign_missing_same_content_and_reparenting(
    evidence_client, asset_data
):
    a, b = asset_data
    first = upload(evidence_client, a, b"one").json()
    second = upload(evidence_client, a, b"two").json()
    foreign = upload(evidence_client, b, b"foreign").json()
    for parent in (str(uuid7()), foreign["id"]):
        result = upload(evidence_client, a, b"new", supersedes_attachment_id=parent)
        assert result.status_code == 404
    assert (
        upload(evidence_client, a, b"one", supersedes_attachment_id=first["id"]).status_code == 422
    )
    assert (
        upload(evidence_client, a, b"two", supersedes_attachment_id=first["id"]).status_code == 409
    )
    assert (
        evidence_client.get(f"/attachments/{second['id']}", headers=headers(a)).json()[
            "supersedes_attachment_id"
        ]
        is None
    )


def test_links_are_immutable_deduplicated_attributed_and_target_checked(
    evidence_client, asset_data, migrator_connection
):
    a, b = asset_data
    row = upload(evidence_client, a).json()
    first = link(evidence_client, a, row["id"])
    assert first.status_code == 201, first.text
    assert first.json()["actor_id"] == str(a.actor_id)
    assert link(evidence_client, a, row["id"]).json() == first.json()
    assert link(evidence_client, a, row["id"], entity_id=b.asset_id).status_code == 422
    assert link(evidence_client, a, row["id"], entity_id=uuid7()).status_code == 422
    assert link(evidence_client, b, row["id"]).status_code == 404
    assert link(evidence_client, a, row["id"], entity_type="LOT").status_code == 422
    assert link(evidence_client, a, row["id"], role="EXCEPTION_EVIDENCE").status_code == 422
    assert len(migrator_connection.execute(select(attachment_links)).all()) == 1


@pytest.mark.parametrize("table", ["attachments", "attachment_links"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_runtime_cannot_rewrite_evidence(
    table, operation, evidence_client, asset_data, app_connection
):
    tenant = asset_data[0]
    captured = upload(evidence_client, tenant).json()
    assert link(evidence_client, tenant, captured["id"]).status_code == 201
    statement = {
        "UPDATE": f"UPDATE fleetops.{table} SET id=id",
        "DELETE": f"DELETE FROM fleetops.{table}",
        "TRUNCATE": f"TRUNCATE fleetops.{table}",
    }[operation]
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError) as failure:
            app_connection.execute(text(statement))
        assert failure.value.orig.sqlstate == "42501"
    finally:
        app_connection.rollback()


@pytest.mark.parametrize("column", ["captured_by", "recorded_at"])
def test_direct_sql_cannot_select_capture_authority(
    column, evidence_client, asset_data, app_connection
):
    a = asset_data[0]
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        value = ":actor" if column == "captured_by" else "'2000-01-01Z'::timestamptz"
        with pytest.raises(DBAPIError) as failure:
            app_connection.execute(
                text(f"""
              INSERT INTO fleetops.attachments
              (id,org_id,sha256,byte_size,media_type,storage_key,source_type,
               captured_at,original_filename,{column})
              VALUES (:id,:org,:hash,1,'text/plain',:key,'DOCUMENT',now(),'x',{value})
            """),
                dict(
                    id=uuid7(),
                    org=a.org_id,
                    hash="a" * 64,
                    key=f"{a.org_id}/{'a' * 64}",
                    actor=a.actor_id,
                ),
            )
        assert failure.value.orig.sqlstate == "42501"
    finally:
        app_connection.rollback()


def test_corrupt_or_missing_bytes_cannot_download_link_or_authorize_retirement(
    evidence_client, asset_data, evidence_root
):
    tenant = asset_data[0]
    row = upload(evidence_client, tenant).json()
    assert link(evidence_client, tenant, row["id"]).status_code == 201
    assert transition(evidence_client, tenant, "RECEIVED", "IN_STOCK", 1).status_code == 201
    path = evidence_root / str(tenant.org_id) / row["sha256"]
    path.write_bytes(b"tampered")
    assert (
        evidence_client.get(
            f"/attachments/{row['id']}/content", headers=headers(tenant)
        ).status_code
        == 503
    )
    assert link(evidence_client, tenant, row["id"]).status_code == 503
    assert (
        transition(
            evidence_client, tenant, "IN_STOCK", "RETIRED", 2, evidence_ref=row["id"]
        ).status_code
        == 503
    )
    path.unlink()
    assert (
        evidence_client.get(
            f"/attachments/{row['id']}/content", headers=headers(tenant)
        ).status_code
        == 503
    )
    state = evidence_client.get(f"/assets/{tenant.asset_id}", headers=headers(tenant)).json()
    assert state["version"] == 2 and state["current_state"] == "IN_STOCK"


def test_upload_limit_is_checked_while_streaming(
    database, asset_data, evidence_root, migrator_connection
):
    a = asset_data[0]
    settings = replace(
        database.settings(a.org_id), evidence_root=evidence_root, evidence_upload_limit=3
    )
    with TestClient(create_app(settings)) as client:
        assert upload(client, a, b"four").status_code == 413
        assert upload(client, a, b"yes").status_code == 201
    assert len(migrator_connection.execute(select(attachments)).all()) == 1


def test_duplicate_by_another_human_preserves_capture_but_attributes_new_link(
    evidence_client, asset_data, other_human
):
    first = upload(evidence_client, asset_data[0], b"shared capture").json()
    duplicate = upload(evidence_client, other_human, b"shared capture")
    assert duplicate.status_code == 201 and duplicate.json() == first
    result = link(evidence_client, other_human, first["id"], entity_id=asset_data[0].asset_id)
    assert result.status_code == 201
    assert result.json()["actor_id"] == str(other_human.actor_id)
    assert first["captured_by"] == str(asset_data[0].actor_id)


@pytest.mark.parametrize("table", [attachments, attachment_links])
def test_missing_and_foreign_context_filter_populated_evidence(
    table, evidence_client, asset_data, app_connection
):
    a, b = asset_data
    row = upload(evidence_client, a).json()
    assert link(evidence_client, a, row["id"]).status_code == 201
    with app_connection.begin():
        assert app_connection.execute(select(table)).all() == []
    with app_connection.begin():
        set_authenticated(app_connection, b)
        assert app_connection.execute(select(table)).all() == []
    with app_connection.begin():
        set_authenticated(app_connection, a)
        assert len(app_connection.execute(select(table)).all()) == 1
    with app_connection.begin():
        assert app_connection.execute(select(table)).all() == []


@pytest.mark.parametrize("authority", ["missing", "organization_only", "wrong_credential_org"])
def test_capture_requires_valid_credential_in_matching_organization(
    authority, asset_data, app_connection
):
    from fleetops.db.tenancy import set_organization

    a, b = asset_data
    with pytest.raises(DBAPIError) as failure, app_connection.begin():
        if authority == "organization_only":
            set_organization(app_connection, a.org_id)
        elif authority == "wrong_credential_org":
            set_authenticated(app_connection, b)
            set_organization(app_connection, a.org_id)
        app_connection.execute(
            text("""
            INSERT INTO fleetops.attachments
              (id,org_id,sha256,byte_size,media_type,storage_key,source_type,
               captured_at,original_filename)
            VALUES (:id,:org,:hash,0,'text/plain',:key,'OTHER',now(),'untrusted')
        """),
            dict(id=uuid7(), org=a.org_id, hash="a" * 64, key=f"{a.org_id}/{'a' * 64}"),
        )
    assert failure.value.orig.sqlstate == "42501"
