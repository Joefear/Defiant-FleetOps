"""Database-enforced catalog facts, independent of API validation."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import external_references, items, party_roles
from fleetops.db.tenancy import set_organization
from fleetops.domain.uom import UnitOfMeasure


@pytest.mark.parametrize("revision", ["A", None], ids=["revision", "null-revision"])
def test_catalog_duplicate_insert_is_rejected(revision, catalog_data, item_values, app_connection):
    a, _ = catalog_data
    values = item_values(a, revision=revision)
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        app_connection.execute(items.insert().values(**values))
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(items.insert().values(**{**values, "id": uuid7()}))
    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uq_items_catalog_entry"


@pytest.mark.parametrize("revision", ["A", None], ids=["revision", "null-revision"])
def test_catalog_duplicate_update_is_rejected(revision, catalog_data, item_values, app_connection):
    a, _ = catalog_data
    first = item_values(a, revision=revision)
    second = item_values(a, manufacturer_part_number="OTHER", revision=revision)
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        app_connection.execute(items.insert(), [first, second])
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(
                items.update()
                .where(items.c.id == second["id"])
                .values(
                    manufacturer_part_number=first["manufacturer_part_number"],
                )
            )
    assert error.value.orig.sqlstate == "23505"


def test_catalog_different_revisions_and_manufacturers_are_distinct(
    catalog_data,
    item_values,
    app_connection,
):
    a, _ = catalog_data
    variants = [
        item_values(a, revision=None),
        item_values(a, revision="A"),
        item_values(a, revision="B"),
        item_values(a, manufacturer_party_id=a.other_manufacturer_id, revision="A"),
    ]
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        app_connection.execute(items.insert(), variants)
        assert set(
            app_connection.execute(
                select(items.c.id).where(items.c.manufacturer_part_number == "NEW-100")
            ).scalars()
        ) == {row["id"] for row in variants}


@pytest.mark.parametrize("target", ["cross-org", "vendor-only", "missing"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_catalog_manufacturer_must_be_same_tenant_member(
    target,
    operation,
    catalog_data,
    item_values,
    app_connection,
):
    a, b = catalog_data
    manufacturer = {"cross-org": b.party_id, "vendor-only": a.vendor_id, "missing": uuid7()}[target]
    # The row itself belongs to A and passes RLS; 23503 proves the relationship boundary.
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            statement = (
                items.insert().values(**item_values(a, manufacturer_party_id=manufacturer))
                if operation == "insert"
                else items.update()
                .where(items.c.id == a.item_id)
                .values(manufacturer_party_id=manufacturer)
            )
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "23503"
    assert error.value.orig.diag.constraint_name in {
        "fk_items_manufacturer_party",
        "fk_items_manufacturer_membership",
    }


@pytest.mark.parametrize("operation", ["delete", "retype"])
def test_catalog_used_manufacturer_membership_cannot_be_removed(
    operation,
    catalog_data,
    migrator_connection,
    catalog_snapshot,
):
    a, _ = catalog_data
    before = catalog_snapshot(items, a.item_id)
    # Even the owner cannot remove this membership without resolving the dependent item.
    with pytest.raises(DBAPIError) as error:
        statement = (
            party_roles.delete().where(party_roles.c.id == a.role_id)
            if operation == "delete"
            else party_roles.update().where(party_roles.c.id == a.role_id).values(role="VENDOR")
        )
        migrator_connection.execute(statement)
    assert error.value.orig.sqlstate == "23503"
    assert error.value.orig.diag.constraint_name == "fk_items_manufacturer_membership"
    migrator_connection.rollback()
    assert catalog_snapshot(items, a.item_id) == before
    assert catalog_snapshot(party_roles, a.role_id)["role"] == "MANUFACTURER"


def test_catalog_manufacturer_discriminator_cannot_be_supplied(
    catalog_data,
    item_values,
    app_connection,
):
    a, _ = catalog_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(
                items.insert().values(
                    **item_values(a, manufacturer_party_id=a.vendor_id),
                    manufacturer_role="VENDOR",
                )
            )
    assert error.value.orig.sqlstate == "428C9"


def test_catalog_uom_check_matches_all_approved_units(catalog_data, item_values, app_connection):
    a, _ = catalog_data
    assert {unit.value for unit in UnitOfMeasure} == {
        "EA",
        "M",
        "MM",
        "CM",
        "IN",
        "FT",
        "G",
        "MG",
        "KG",
        "ML",
        "L",
    }
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        for unit in UnitOfMeasure:
            app_connection.execute(
                items.insert().values(
                    **item_values(a, manufacturer_part_number=f"UOM-{unit}", uom=unit),
                )
            )
        assert set(
            app_connection.execute(
                select(items.c.uom).where(items.c.manufacturer_part_number.like("UOM-%"))
            ).scalars()
        ) == set(UnitOfMeasure)


@pytest.mark.parametrize("unit", ["REEL", "TRAY", "TUBE", "BOX", "UNKNOWN"])
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_catalog_invalid_uom_is_rejected_by_database(
    unit,
    operation,
    catalog_data,
    item_values,
    app_connection,
):
    a, _ = catalog_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            statement = (
                items.insert().values(**item_values(a, uom=unit))
                if operation == "insert"
                else items.update().where(items.c.id == a.item_id).values(uom=unit)
            )
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "23514"
    assert error.value.orig.diag.constraint_name == "ck_items_uom"


@pytest.mark.parametrize(
    "controlled,classification", [(False, None), (True, None), (True, "3A001")]
)
def test_catalog_classification_is_capture_only(
    controlled,
    classification,
    catalog_data,
    item_values,
    app_connection,
):
    a, _ = catalog_data
    with app_connection.begin():
        set_organization(app_connection, a.org_id)
        row = (
            app_connection.execute(
                items.insert()
                .values(
                    **item_values(
                        a, export_controlled=controlled, export_classification=classification
                    ),
                )
                .returning(items)
            )
            .mappings()
            .one()
        )
        assert row["export_controlled"] is controlled
        assert row["export_classification"] == classification


def test_catalog_duplicate_reference_attachment_is_rejected(catalog_data, app_connection):
    a, _ = catalog_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(
                external_references.insert().values(
                    id=uuid7(),
                    org_id=a.org_id,
                    entity_type="ITEM",
                    entity_id=a.item_id,
                    system="DigiKey",
                    reference_type="DISTRIBUTOR_PART",
                    external_value="123-456",
                    created_by_actor_id=a.actor_id,
                )
            )
    assert error.value.orig.sqlstate == "23505"
    assert error.value.orig.diag.constraint_name == "uq_external_references_attachment"


@pytest.mark.parametrize("kind", ["USER", "SESSION", "ASSET", "ACTOR", "ORGANIZATION"])
def test_catalog_reference_type_check_rejects_unopened_targets(kind, catalog_data, app_connection):
    a, _ = catalog_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            app_connection.execute(
                external_references.insert().values(
                    id=uuid7(),
                    org_id=a.org_id,
                    entity_type=kind,
                    entity_id=a.item_id,
                    system="probe",
                    reference_type="code",
                    external_value="value",
                    created_by_actor_id=a.actor_id,
                )
            )
    assert error.value.orig.sqlstate == "23514"


@pytest.mark.parametrize(
    "table,operation",
    [(items, "creator-insert"), (external_references, "creator-insert"), (items, "updater-update")],
    ids=["item-creator", "reference-creator", "item-updater"],
)
def test_catalog_attribution_foreign_keys_cannot_cross_tenants(
    table,
    operation,
    catalog_data,
    item_values,
    app_connection,
):
    a, b = catalog_data
    with pytest.raises(DBAPIError) as error:
        with app_connection.begin():
            set_organization(app_connection, a.org_id)
            if operation == "updater-update":
                statement = (
                    items.update()
                    .where(items.c.id == a.item_id)
                    .values(updated_by_actor_id=b.actor_id)
                )
            elif table is items:
                statement = items.insert().values(**item_values(a, created_by_actor_id=b.actor_id))
            else:
                statement = table.insert().values(
                    id=uuid7(),
                    org_id=a.org_id,
                    entity_type="ITEM",
                    entity_id=a.item_id,
                    system="probe",
                    reference_type="code",
                    external_value="value",
                    created_by_actor_id=b.actor_id,
                )
            app_connection.execute(statement)
    assert error.value.orig.sqlstate == "23503"
