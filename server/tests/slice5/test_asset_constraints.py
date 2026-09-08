"""Durable identity, initialization, evidence and tenant-reference constraints."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_identifiers, asset_transitions, assets, items
from fleetops.db.tenancy import set_organization


def rejected(connection, statement, code, org_id=None):
    """A specific PostgreSQL error proves the intended constraint, not a SQL typo."""
    with pytest.raises(DBAPIError) as error:
        with connection.begin():
            if org_id is not None:
                set_organization(connection, org_id)
            connection.execute(statement)
    assert error.value.orig.sqlstate == code


def test_initial_pair_and_asset_tag_identity(asset_data, snapshot, seed_asset, migrator_connection):
    a, b = asset_data
    for tenant in (a, b):
        row, history = snapshot(tenant)
        assert row["id"].version == 7
        assert row["version"] == history[0]["result_version"] == 1
        assert row["current_state"] == history[0]["to_state"] == "RECEIVED"
        assert history[0]["from_state"] is None
        assert row["asset_tag"] == "FLEET-001"
        assert row["owner_party_id"] is not None
    row, _ = snapshot(a)
    row.pop("item_serialized")
    rejected(migrator_connection, assets.insert().values(row | {"id": uuid7()}), "23505")
    assert seed_asset(a)["id"] != a.asset_id


@pytest.mark.parametrize(
    "field",
    [
        "item_id",
        "owner_party_id",
        "custodian_party_id",
        "current_location_id",
        "created_by_actor_id",
        "updated_by_actor_id",
    ],
)
def test_asset_cross_tenant_references_reject(
    field, asset_data, asset_dml, app_connection, migrator_connection
):
    a, b = asset_data
    value = {
        "item_id": b.item_id,
        "owner_party_id": b.party_id,
        "custodian_party_id": b.party_id,
        "current_location_id": b.location_id,
        "created_by_actor_id": b.actor_id,
        "updated_by_actor_id": b.actor_id,
    }[field]
    # Updater fields are overwritten by the runtime guard; test their composite FK
    # with an owner connection as well as proving runtime cannot persist the spoof.
    if field == "updated_by_actor_id":
        from server.tests.auth_context import set_authenticated

        with app_connection.begin():
            set_authenticated(app_connection, a)
            row = app_connection.execute(
                assets.update()
                .where(assets.c.id == a.asset_id)
                .values(updated_by_actor_id=value)
                .returning(assets.c.updated_by_actor_id)
            ).scalar_one()
            assert row == a.actor_id
        rejected(
            migrator_connection,
            assets.update().where(assets.c.id == a.asset_id).values(updated_by_actor_id=value),
            "23503",
        )
        return
    rejected(
        app_connection,
        assets.update().where(assets.c.id == a.asset_id).values(**{field: value}),
        "23503",
        a.org_id,
    )


def test_serialized_item_invariant_survives_item_and_asset_updates(asset_data, migrator_connection):
    a, _ = asset_data
    rejected(
        migrator_connection,
        items.update().where(items.c.id == a.item_id).values(serialized=False),
        "23503",
    )
    item = dict(
        migrator_connection.execute(select(items).where(items.c.id == a.item_id)).mappings().one()
    )
    item.pop("manufacturer_role")
    nonserialized = uuid7()
    migrator_connection.execute(
        items.insert().values(
            item
            | dict(id=nonserialized, manufacturer_part_number="NON-SERIALIZED", serialized=False)
        )
    )
    migrator_connection.commit()
    rejected(
        migrator_connection,
        assets.update().where(assets.c.id == a.asset_id).values(item_id=nonserialized),
        "23503",
    )
    row = dict(
        migrator_connection.execute(select(assets).where(assets.c.id == a.asset_id))
        .mappings()
        .one()
    )
    row.pop("item_serialized")
    migrator_connection.rollback()
    rejected(
        migrator_connection,
        assets.insert().values(row | dict(id=uuid7(), asset_tag="BAD", item_id=nonserialized)),
        "23503",
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"owner_party_id": None}, "23502"),
        ({"version": 0}, "23514"),
        ({"current_state": "ORDERED"}, "23514"),
        ({"current_assignment_id": uuid7()}, "23514"),
    ],
)
def test_asset_required_owner_state_version_and_deferred_assignment(
    changes, code, asset_data, migrator_connection
):
    rejected(
        migrator_connection,
        assets.update().where(assets.c.id == asset_data[0].asset_id).values(**changes),
        code,
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"from_state": "READY"}, "23514"),
        ({"to_state": "READY"}, "23514"),
        ({"result_version": 2}, "23514"),
        ({"result_version": 0}, "23514"),
        ({"result_version": -1}, "23514"),
        ({"result_version": None}, "23502"),
        ({}, "23505"),
        ({"evidence_ref": uuid7()}, "23514"),
        ({"to_state": "ORDERED"}, "23514"),
    ],
)
def test_initial_transition_constraints(changes, code, asset_data, snapshot, migrator_connection):
    _, history = snapshot(asset_data[0])
    rejected(
        migrator_connection,
        asset_transitions.insert().values(history[0] | {"id": uuid7()} | changes),
        code,
    )


def test_correction_reference_is_tenant_and_asset_safe(
    asset_data, seed_asset, snapshot, migrator_connection
):
    a, b = asset_data
    _, history = snapshot(a)
    _, other_history = snapshot(b)
    other_asset = seed_asset(a)
    same_org_other = migrator_connection.execute(
        select(asset_transitions.c.id).where(asset_transitions.c.asset_id == other_asset["id"])
    ).scalar_one()
    migrator_connection.rollback()
    for target in (other_history[0]["id"], same_org_other):
        rejected(
            migrator_connection,
            asset_transitions.insert().values(
                history[0]
                | dict(
                    id=uuid7(),
                    result_version=2,
                    from_state="RECEIVED",
                    to_state="IN_STOCK",
                    corrects_transition_id=target,
                )
            ),
            "23503",
        )


@pytest.mark.parametrize(
    "value,reason,valid",
    [
        (None, "Label damaged", True),
        (None, None, False),
        (None, "  ", False),
        (None, "\t\n", False),
        ("READABLE", "Damaged", False),
        ("", None, False),
        ("NEW", None, True),
    ],
)
def test_identifier_readability(value, reason, valid, asset_data, migrator_connection):
    a, _ = asset_data
    statement = asset_identifiers.insert().values(
        id=uuid7(),
        org_id=a.org_id,
        asset_id=a.asset_id,
        type="OTHER",
        value=value,
        unreadable_reason=reason,
        created_by_actor_id=a.actor_id,
    )
    if valid:
        with migrator_connection.begin():
            migrator_connection.execute(statement)
    else:
        rejected(migrator_connection, statement, "23514")


def test_identifier_uniqueness_and_multiple_unreadable_markers(
    asset_data, seed_asset, migrator_connection
):
    a, _ = asset_data  # Both seeded tenants already share MANUFACTURER_SERIAL / SERIAL-001.
    other = seed_asset(a)
    values = dict(
        org_id=a.org_id,
        asset_id=other["id"],
        type="MANUFACTURER_SERIAL",
        value="SERIAL-001",
        created_by_actor_id=a.actor_id,
    )
    rejected(migrator_connection, asset_identifiers.insert().values(id=uuid7(), **values), "23505")
    with migrator_connection.begin():
        migrator_connection.execute(
            asset_identifiers.insert().values(values | dict(id=uuid7(), type="PCB_SERIAL"))
        )
        for target in (a.asset_id, other["id"]):
            migrator_connection.execute(
                asset_identifiers.insert().values(
                    values
                    | dict(
                        id=uuid7(), asset_id=target, value=None, unreadable_reason="Label obscured"
                    )
                )
            )


@pytest.mark.parametrize(
    "table,field",
    [
        (asset_identifiers, "asset_id"),
        (asset_identifiers, "created_by_actor_id"),
        (asset_transitions, "asset_id"),
        (asset_transitions, "actor_id"),
    ],
)
def test_history_and_identifier_tenant_fks(table, field, asset_data, asset_dml, app_connection):
    a, b = asset_data
    value = b.asset_id if field == "asset_id" else b.actor_id
    rejected(
        app_connection,
        table.update().where(table.c.org_id == a.org_id).values(**{field: value}),
        "23503",
        a.org_id,
    )
