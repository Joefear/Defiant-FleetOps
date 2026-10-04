"""Exact 0010 restoration includes data, catalogs, view security, roles and sequences."""

from server.tests.auth_context import set_authenticated
from server.tests.migration_snapshot import schema_snapshot
from server.tests.slice6.test_fact_migration import migrate, role_snapshot
from server.tests.slice7.test_migration import sequence_snapshot
from server.tests.slice9.conftest import WHEN
from sqlalchemy import create_engine, text
from uuid6 import uuid7


def round_trip(database, connection, *, historical=None):
    """Compare against a pre-0011 snapshot so symmetric downgrade bugs cannot self-validate."""
    head = schema_snapshot(connection)
    roles = role_snapshot(connection)
    sequence = sequence_snapshot(connection)
    try:
        migrate(database, "check")
        migrate(database, "downgrade", "0010_receiving")
        predecessor = schema_snapshot(connection)
        if historical is not None:
            assert predecessor == historical
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
        migrate(database, "upgrade", "head")
        assert schema_snapshot(connection) == head
        migrate(database, "check")
        migrate(database, "downgrade", "0010_receiving")
        assert schema_snapshot(connection) == predecessor
        migrate(database, "upgrade", "head")
        assert schema_snapshot(connection) == head
        assert role_snapshot(connection) == roles
        assert sequence_snapshot(connection) == sequence
    finally:
        connection.rollback()
        migrate(database, "upgrade", "head")


def test_fresh_exact_0010_round_trip(fresh_database):
    engine = create_engine(fresh_database.url("fleetops_migrator"))
    try:
        with engine.connect() as connection:
            round_trip(fresh_database, connection, historical=fresh_database.historical_0010)
    finally:
        engine.dispose()


def test_populated_exact_0010_round_trip(database, asset_data, make_order, migrator_connection):
    make_order()
    round_trip(database, migrator_connection)


def test_pre_0011_receiving_observations_gain_only_open_projection(
    database,
    space_data,
    make_item,
    app_connection,
    migrator_connection,
):
    """Seed genuine predecessor SQL, then prove two complete populated round trips."""
    tenant = space_data[0]
    item = make_item()
    receipt_id, line_id = uuid7(), uuid7()
    identifiers = [uuid7(), uuid7()]
    try:
        migrate(database, "downgrade", "0010_receiving")
        with app_connection.begin():
            set_authenticated(app_connection, tenant)
            # Use predecessor runtime inputs; its triggers derive serialization and times.
            app_connection.execute(
                text("""
                INSERT INTO fleetops.receipts
                  (id,org_id,vendor_party_id,dock_location_id,received_at)
                VALUES (:id,:org,:vendor,:dock,:when)
            """),
                dict(
                    id=receipt_id,
                    org=tenant.org_id,
                    vendor=tenant.vendor_id,
                    dock=tenant.location_id,
                    when=WHEN,
                ),
            )
            app_connection.execute(
                text("""
                INSERT INTO fleetops.receipt_lines
                  (id,org_id,receipt_id,item_id,quantity,uom,condition)
                VALUES (:id,:org,:receipt,:item,1,'EA','DAMAGED')
            """),
                dict(id=line_id, org=tenant.org_id, receipt=receipt_id, item=item, when=WHEN),
            )
            for identifier, kind in zip(identifiers, ("DAMAGED", "UNEXPECTED_ITEM"), strict=True):
                app_connection.execute(
                    text("""
                    INSERT INTO fleetops.receiving_exceptions
                      (id,org_id,receipt_id,receipt_line_id,exception_type)
                    VALUES (:id,:org,:receipt,:line,:kind)
                """),
                    dict(
                        id=identifier,
                        org=tenant.org_id,
                        receipt=receipt_id,
                        line=line_id,
                        kind=kind,
                        when=WHEN,
                    ),
                )
            app_connection.execute(
                text("""
                INSERT INTO fleetops.receipt_reconciliations (id,org_id,receipt_id)
                VALUES (:id,:org,:receipt)
            """),
                dict(id=uuid7(), org=tenant.org_id, receipt=receipt_id, when=WHEN),
            )
        observations = (
            migrator_connection.execute(
                text("""
            SELECT * FROM fleetops.receiving_exceptions WHERE receipt_id=:receipt ORDER BY id
        """),
                dict(receipt=receipt_id),
            )
            .mappings()
            .all()
        )
        assert len(observations) == 2
        columns = ",".join(observations[0].keys())
        migrator_connection.rollback()
        predecessor = schema_snapshot(migrator_connection)
        for _ in range(2):
            migrate(database, "upgrade", "head")
            migrate(database, "check")
            with app_connection.begin():
                set_authenticated(app_connection, tenant)
                assert (
                    app_connection.execute(
                        text(f"""
                    SELECT {columns} FROM fleetops.receiving_exceptions
                    WHERE receipt_id=:receipt ORDER BY id
                """),
                        dict(receipt=receipt_id),
                    )
                    .mappings()
                    .all()
                    == observations
                )
                assert (
                    app_connection.execute(
                        text("""
                    SELECT count(*) FROM fleetops.receiving_exceptions
                    WHERE receipt_id=:receipt AND evaluation_id IS NOT NULL
                """),
                        dict(receipt=receipt_id),
                    ).scalar_one()
                    == 0
                )
                for observation in observations:
                    workflow = (
                        app_connection.execute(
                            text("""
                        SELECT * FROM fleetops.exception_workflows WHERE exception_id=:id
                    """),
                            dict(id=observation["id"]),
                        )
                        .mappings()
                        .one()
                    )
                    assert workflow["status"] == "OPEN"
                    assert workflow["event_seq"] == 0
                    assert workflow["severity"] == "UNSPECIFIED"
                    assert workflow["exception_type"] == observation["exception_type"]
                    assert workflow["entity_type"] == "RECEIPT_LINE"
                    assert workflow["entity_id"] == line_id
                    assert all(
                        workflow[key] is None
                        for key in (
                            "resolved_by_actor_id",
                            "resolved_at",
                            "resolution_note",
                        )
                    )
                    assert (
                        app_connection.execute(
                            text("""
                        SELECT count(*) FROM fleetops.exception_events WHERE exception_id=:id
                    """),
                            dict(id=observation["id"]),
                        ).scalar_one()
                        == 0
                    )
            migrate(database, "downgrade", "0010_receiving")
            assert schema_snapshot(migrator_connection) == predecessor
            assert (
                migrator_connection.execute(
                    text("""
                SELECT * FROM fleetops.receiving_exceptions WHERE receipt_id=:receipt ORDER BY id
            """),
                    dict(receipt=receipt_id),
                )
                .mappings()
                .all()
                == observations
            )
            migrator_connection.rollback()
    finally:
        migrator_connection.rollback()
        migrate(database, "upgrade", "head")
