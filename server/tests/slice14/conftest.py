"""Slice 14 reuses real PostgreSQL fixtures; no client substitute database."""

from server.tests.slice5 import conftest as asset_fixtures
from server.tests.slice9 import conftest as receiving_fixtures

space_data = asset_fixtures.space_data
space_password_hash = asset_fixtures.space_password_hash
seed_asset = asset_fixtures.seed_asset
asset_data = asset_fixtures.asset_data
asset_client = asset_fixtures.asset_client
receiving_cleanup = receiving_fixtures.receiving_cleanup
make_item = receiving_fixtures.make_item
make_order = receiving_fixtures.make_order
