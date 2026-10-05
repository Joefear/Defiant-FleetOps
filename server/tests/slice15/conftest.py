"""Disposable PostgreSQL, real bearer identities and test-owned output directories."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from server.tests.slice5.conftest import seed_asset
from server.tests.slice9 import conftest as receiving

from fleetops.api.app import create_app

space_data = receiving.space_data
space_password_hash = receiving.space_password_hash
asset_data = receiving.asset_data
make_item = receiving.make_item
make_order = receiving.make_order
__all__ = ["seed_asset"]


@pytest.fixture
def receiving_cleanup(space_data, migrator_connection, app_connection):
    """Opt in to existing privileged disposal without changing unrelated test fixtures."""
    yield from receiving.receiving_cleanup.__wrapped__(
        space_data, migrator_connection, app_connection
    )


@pytest.fixture
def scenario_client(database, space_data, tmp_path, receiving_cleanup):
    settings = replace(
        database.settings(space_data[0].org_id),
        evidence_root=tmp_path / "evidence",
        label_output_root=tmp_path / "labels",
    )
    with TestClient(create_app(settings)) as client:
        yield client
