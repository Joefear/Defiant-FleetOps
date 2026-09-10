"""Ordinary RLS-protected configuration appends; allocation order is not Asset authority."""

from uuid import UUID

from sqlalchemy import Connection, select
from uuid6 import uuid7

from fleetops.db.metadata import asset_configurations
from fleetops.domain.assets import _constraints, get_asset


def append_configuration(connection: Connection, asset_id: UUID, *, org_id: UUID, values: dict):
    """Column grants force Actor, recording time and sequence to their database defaults.

    The organization comes from authenticated request context, never from request JSON.
    No Asset row lock/version precondition or unnecessary elevated function is involved.
    """
    get_asset(connection, asset_id)
    with _constraints():
        return (
            connection.execute(
                asset_configurations.insert()
                .values(
                    **values,
                    id=uuid7(),
                    asset_id=asset_id,
                    org_id=org_id,
                )
                .returning(asset_configurations)
            )
            .mappings()
            .one()
        )


def list_configurations(connection: Connection, asset_id: UUID):
    """Read deterministic per-Asset order; response models omit the global sequence value."""
    get_asset(connection, asset_id)
    return (
        connection.execute(
            select(asset_configurations)
            .where(asset_configurations.c.asset_id == asset_id)
            .order_by(asset_configurations.c.configuration_seq)
        )
        .mappings()
        .all()
    )


def current_configuration(connection: Connection, asset_id: UUID):
    """Greatest visible allocation wins, even if another transaction commits later."""
    get_asset(connection, asset_id)
    return (
        connection.execute(
            select(asset_configurations)
            .where(asset_configurations.c.asset_id == asset_id)
            .order_by(asset_configurations.c.configuration_seq.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
