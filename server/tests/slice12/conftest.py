"""Use real authenticated tenants and dedicated FleetOps-local output paths."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from server.tests.slice5.conftest import asset_data, seed_asset, space_data, space_password_hash
from server.tests.slice9 import conftest as receiving_fixtures

from fleetops.api.app import create_app

make_item = receiving_fixtures.make_item
make_order = receiving_fixtures.make_order


@pytest.fixture
def receiving_cleanup(space_data, migrator_connection, app_connection):
    yield from receiving_fixtures.receiving_cleanup.__wrapped__(
        space_data, migrator_connection, app_connection
    )


__all__ = [
    "asset_data",
    "seed_asset",
    "space_data",
    "space_password_hash",
    "make_item",
    "make_order",
    "receiving_cleanup",
]


def headers(tenant):
    return {"Authorization": f"Bearer {tenant.raw_token}"}


def template(client, tenant, **changes):
    """Create the actual immutable template through the authenticated endpoint."""
    return client.post(
        "/label-templates",
        headers=headers(tenant),
        json={
            "name": "Factory asset",
            "human_fields": ["id", "asset_tag", "item_mpn"],
            "symbology": "CODE128",
            **changes,
        },
    )


def queue(client, tenant, template_id, *, output_format="PNG", asset_id=None, **changes):
    return client.post(
        f"/assets/{asset_id or tenant.asset_id}/labels",
        headers=headers(tenant),
        json={"template_id": str(template_id), "output_format": output_format, **changes},
    )


@pytest.fixture
def label_root(tmp_path):
    return tmp_path / "label-output"


@pytest.fixture
def label_client(database, asset_data, label_root):
    """Queue and dispatch use separate production request transactions."""
    settings = replace(database.settings(asset_data[0].org_id), label_output_root=label_root)
    with TestClient(create_app(settings)) as client:
        yield client
