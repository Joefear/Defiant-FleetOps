"""Prove database tenancy and privilege boundaries as actual fleetops_app."""

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice12.conftest import queue, template
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import label_templates, print_jobs
from fleetops.db.tenancy import set_organization


@pytest.mark.parametrize("table", [label_templates, print_jobs])
def test_new_tables_fail_closed_and_scope_reads(label_client, asset_data, app_connection, table):
    a, b = asset_data
    for tenant in (a, b):
        record = template(label_client, tenant).json()
        assert queue(label_client, tenant, record["id"]).status_code == 202
    try:
        app_connection.begin()
        assert app_connection.execute(select(table)).all() == []
        app_connection.rollback()
        app_connection.begin()
        set_authenticated(app_connection, a)
        rows = app_connection.execute(select(table)).mappings().all()
        assert len(rows) == 1 and rows[0]["org_id"] == a.org_id
    finally:
        app_connection.rollback()


@pytest.mark.parametrize("table", ["label_templates", "print_jobs"])
@pytest.mark.parametrize("operation", ["DELETE", "TRUNCATE", "UPDATE_ID"])
def test_runtime_cannot_discard_or_rewrite_requests(
    label_client, asset_data, app_connection, table, operation
):
    tenant = asset_data[0]
    record = template(label_client, tenant).json()
    queue(label_client, tenant, record["id"])
    statement = {
        "DELETE": f"DELETE FROM fleetops.{table}",
        "TRUNCATE": f"TRUNCATE fleetops.{table}",
        "UPDATE_ID": f"UPDATE fleetops.{table} SET id=id",
    }[operation]
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError) as failure:
            app_connection.execute(text(statement))
        assert failure.value.orig.sqlstate == "42501"
    finally:
        app_connection.rollback()


@pytest.mark.parametrize("column", ["created_by_actor_id", "created_at"])
def test_direct_sql_cannot_forge_template_capture(label_client, asset_data, app_connection, column):
    tenant = asset_data[0]
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        value = ":actor" if column == "created_by_actor_id" else "'1970-01-01'"
        with pytest.raises(DBAPIError) as failure:
            app_connection.execute(
                text(
                    f"INSERT INTO fleetops.label_templates "
                    f"(id,org_id,name,entity_type,human_fields,symbology,{column}) "
                    f"VALUES (:id,:org,'Label','ASSET',jsonb_build_array('id'),'CODE128',{value})"
                ),
                {"id": uuid7(), "org": tenant.org_id, "actor": tenant.actor_id},
            )
        assert failure.value.orig.sqlstate == "42501"
    finally:
        app_connection.rollback()


@pytest.mark.parametrize("context", ["none", "org_only", "foreign_org"])
def test_template_insert_requires_matching_live_credential(
    label_client, asset_data, app_connection, context
):
    a, b = asset_data
    try:
        app_connection.begin()
        if context == "org_only":
            set_organization(app_connection, a.org_id)
        if context == "foreign_org":
            set_authenticated(app_connection, b)
            set_organization(app_connection, a.org_id)
        with pytest.raises(DBAPIError):
            app_connection.execute(
                label_templates.insert().values(
                    id=uuid7(),
                    org_id=a.org_id,
                    name="Missing authority",
                    entity_type="ASSET",
                    human_fields=["id"],
                    symbology="CODE128",
                )
            )
    finally:
        app_connection.rollback()


def test_sql_template_cannot_select_a_composite_barcode(label_client, asset_data, app_connection):
    tenant = asset_data[0]
    try:
        app_connection.begin()
        set_authenticated(app_connection, tenant)
        with pytest.raises(DBAPIError) as failure:
            app_connection.execute(
                label_templates.insert().values(
                    id=uuid7(),
                    org_id=tenant.org_id,
                    name="Composite",
                    entity_type="ASSET",
                    human_fields=["asset_tag"],
                    symbology="DATAMATRIX",
                    barcode_field="asset_tag",
                )
            )
        assert failure.value.orig.sqlstate == "23514"
    finally:
        app_connection.rollback()


@pytest.mark.parametrize("foreign", ["template", "asset", "org"])
def test_sql_queue_rejects_cross_tenant_relationships(
    label_client, asset_data, app_connection, foreign
):
    a, b = asset_data
    a_template = template(label_client, a).json()
    b_template = template(label_client, b).json()
    try:
        app_connection.begin()
        set_authenticated(app_connection, a)
        with pytest.raises(DBAPIError):
            app_connection.execute(
                print_jobs.insert().values(
                    id=uuid7(),
                    org_id=b.org_id if foreign == "org" else a.org_id,
                    template_id=b_template["id"] if foreign == "template" else a_template["id"],
                    entity_id=b.asset_id if foreign == "asset" else a.asset_id,
                    adapter_name="FILE",
                    output_format="PNG",
                )
            )
    finally:
        app_connection.rollback()
