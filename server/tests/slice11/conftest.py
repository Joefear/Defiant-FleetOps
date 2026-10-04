"""Real authenticated tenants, isolated storage and the existing PostgreSQL harness."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from server.tests.slice5.conftest import (
    asset_data,
    seed_asset,
    space_data,
    space_password_hash,
)
from server.tests.slice5.test_authentication_boundary import other_human

from fleetops.api.app import create_app

__all__ = ["asset_data", "seed_asset", "space_data", "space_password_hash", "other_human"]

WHEN = "2026-01-02T03:04:05Z"


def headers(tenant):
    """Use the fixture's real bearer credential, not a tenant/Actor assertion."""
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def upload(client, tenant, content=b"disposal certificate", **changes):
    """Capture raw bytes with explicit provenance and optional adversarial changes."""
    query = dict(
        source_type="DOCUMENT",
        captured_at=WHEN,
        original_filename="record.txt",
        media_type="text/plain",
    )
    query.update(changes)
    return client.post("/attachments", params=query, content=content, headers=headers(tenant))


def link(
    client, tenant, attachment_id, *, role="DISPOSAL_EVIDENCE", entity_id=None, entity_type="ASSET"
):
    """Link one capture to an existing entity through the public authenticated route."""
    return client.post(
        f"/attachments/{attachment_id}/links",
        headers=headers(tenant),
        json=dict(
            entity_type=entity_type, entity_id=str(entity_id or tenant.asset_id), link_role=role
        ),
    )


def transition(client, tenant, source, target, version, **changes):
    """Exercise Python graph and database admission together."""
    return client.post(
        f"/assets/{tenant.asset_id}/transitions",
        headers=headers(tenant),
        json=dict(
            expected_version=version,
            from_state=source,
            to_state=target,
            reason="Physical lifecycle observation",
            occurred_at=WHEN,
            **changes,
        ),
    )


@pytest.fixture
def evidence_root(tmp_path):
    """Each test owns its storage; the invocation supplies a FleetOps-local basetemp."""
    return tmp_path / "evidence"


@pytest.fixture
def evidence_client(database, asset_data, evidence_root):
    """Use separate runtime/login pools and a real local filesystem adapter."""
    settings = replace(database.settings(asset_data[0].org_id), evidence_root=evidence_root)
    with TestClient(create_app(settings)) as client:
        yield client
