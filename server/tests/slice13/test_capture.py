"""End-to-end contract, authenticated claims, tenant isolation and independent outcomes."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from server.tests.slice13.conftest import headers, operation, submit
from sqlalchemy import select, text

from fleetops.db import metadata as db


def test_batch_replay_three_times_has_identical_results_and_single_effect(
    capture_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    first = operation(tenant)
    second = operation(
        tenant,
        client_id=first["client_id"],
        client_epoch=first["client_epoch"],
        client_seq=2,
        expected_version=2,
        operation="TRANSITION",
        payload={"from_state": "RECEIVED", "to_state": "IN_STOCK", "reason": "Placed"},
    )
    rows = [submit(capture_client, tenant, [second, first]) for _ in range(3)]
    assert [row["sync_state"] for row in rows[0]] == ["APPLIED", "APPLIED"]
    assert [row["sync_state"] for row in rows[1]] == ["DUPLICATE", "DUPLICATE"]
    for repeated in rows[1:]:
        assert [row["result"] for row in repeated] == [row["result"] for row in rows[0]]
        assert [row["recorded_at"] for row in repeated] == [row["recorded_at"] for row in rows[0]]
    actual = (
        migrator_connection.execute(select(db.assets).where(db.assets.c.id == tenant.asset_id))
        .mappings()
        .one()
    )
    assert (actual["version"], actual["current_state"], actual["current_location_id"]) == (
        3,
        "IN_STOCK",
        None,
    )
    moves = (
        migrator_connection.execute(
            select(db.asset_movements).where(db.asset_movements.c.asset_id == tenant.asset_id)
        )
        .mappings()
        .all()
    )
    assert len(moves) == 1 and moves[0]["client_op_id"] == UUID(first["operation_id"])
    assert moves[0]["occurred_at"] == datetime(1970, 1, 1, tzinfo=UTC)
    assert moves[0]["recorded_at"].year >= 2026
    persisted = (
        migrator_connection.execute(
            select(db.capture_operations).where(
                db.capture_operations.c.operation_id == UUID(first["operation_id"])
            )
        )
        .mappings()
        .one()
    )
    assert persisted["actor_id"] == tenant.actor_id and persisted["occurred_at"].year == 1970
    assert persisted["recorded_at"].year >= 2026


def test_two_scanner_v17_v18_conflict_preserves_line2(
    capture_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    for version in range(1, 17):
        submit(
            capture_client,
            tenant,
            [
                operation(
                    tenant,
                    expected_version=version,
                    payload={
                        "to_location_id": str(tenant.location_id),
                        "reason": "Stockroom witness",
                    },
                )
            ],
        )
    line1 = uuid4()
    migrator_connection.execute(
        db.locations.insert().values(
            id=line1,
            org_id=tenant.org_id,
            facility_id=tenant.facility_id,
            code="LINE-1",
            name="Line 1",
            kind="STATION",
            created_by_actor_id=tenant.actor_id,
        )
    )
    migrator_connection.commit()
    offline = operation(
        tenant,
        expected_version=17,
        payload={"to_location_id": str(line1), "reason": "Offline Line 1"},
    )
    online = operation(
        tenant, expected_version=17, payload={"to_location_id": None, "reason": "Online Line 2"}
    )
    # A second real same-tenant location names Line 2 rather than making NULL a destination.
    loc = tenant.other_location_id
    assert len({line1, loc, tenant.location_id}) == 3
    online["payload"]["to_location_id"] = str(loc)
    assert submit(capture_client, tenant, [online])[0]["sync_state"] == "APPLIED"
    rejected = submit(capture_client, tenant, [offline])[0]
    assert rejected["sync_state"] == "REJECTED" and rejected["result"]["code"] == "SYNC_CONFLICT"
    assert rejected["result"]["expected"]["version"] == 17
    assert rejected["result"]["expected"]["location_id"] == str(tenant.location_id)
    assert rejected["result"]["current"]["version"] == 18
    assert rejected["result"]["current"]["location_id"] == str(loc)
    exception = capture_client.get(
        "/exceptions/" + rejected["result"]["exception_id"], headers=headers(tenant)
    ).json()
    assert exception["exception_type"] == "SYNC_CONFLICT" and exception["entity_type"] == "ASSET"
    assert exception["status"] == "OPEN" and exception["actor_id"] == str(tenant.actor_id)
    open_rows = capture_client.get(
        "/exceptions/open",
        headers=headers(tenant),
        params={"entity_type": "ASSET", "entity_id": str(tenant.asset_id)},
    ).json()
    assert [row["id"] for row in open_rows] == [exception["id"]]
    asset = capture_client.get(f"/assets/{tenant.asset_id}", headers=headers(tenant)).json()
    assert asset["version"] == 18 and asset["current_location_id"] == str(loc)
    assert (
        migrator_connection.execute(
            text("SELECT count(*) FROM fleetops.asset_movements WHERE asset_id=:asset"),
            {"asset": tenant.asset_id},
        ).scalar_one()
        == 17
    )


def test_bad_operation_does_not_roll_back_neighbors(capture_client, asset_data):
    tenant = asset_data[0]
    good = operation(tenant)
    bad = operation(
        tenant,
        client_id=good["client_id"],
        client_epoch=good["client_epoch"],
        client_seq=2,
        expected_version=2,
        payload={"to_location_id": str(uuid4()), "reason": "Unknown location"},
    )
    after = operation(
        tenant,
        client_id=good["client_id"],
        client_epoch=good["client_epoch"],
        client_seq=3,
        expected_version=2,
    )
    rows = submit(capture_client, tenant, [good, bad, after])
    assert [row["sync_state"] for row in rows] == ["APPLIED", "REJECTED", "APPLIED"]
    assert rows[2]["result"]["result_version"] == 3
    assert [row["result"] for row in submit(capture_client, tenant, [good, bad, after])] == [
        row["result"] for row in rows
    ]


def test_epoch_reset_gaps_and_reused_sequences_are_visible(capture_client, asset_data):
    tenant = asset_data[0]
    first = operation(tenant, client_seq=3)
    row = submit(capture_client, tenant, [first])[0]
    assert row["sequence_flags"] == ["SEQUENCE_GAP"]
    same = operation(
        tenant,
        client_id=first["client_id"],
        client_epoch=first["client_epoch"],
        client_seq=3,
        expected_version=2,
    )
    row = submit(capture_client, tenant, [same])[0]
    assert row["sync_state"] == "APPLIED" and row["sequence_flags"] == ["SEQUENCE_REUSED"]
    reset = operation(tenant, client_id=first["client_id"], client_seq=1, expected_version=3)
    row = submit(capture_client, tenant, [reset])[0]
    assert row["sync_state"] == "APPLIED" and row["sequence_flags"] == []
    duplicate = submit(capture_client, tenant, [first])[0]
    assert duplicate["sync_state"] == "DUPLICATE" and duplicate["sequence_flags"] == [
        "SEQUENCE_GAP"
    ]


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("MOVE", {"to_location_id": None, "reason": "Move"}),
        ("ASSIGN", {"assignee_type": "LOCATION", "assignee_id": "TARGET", "reason": "Assign"}),
        ("UNASSIGN", {"reason": "Unassign"}),
        ("TRANSITION", {"from_state": "RECEIVED", "to_state": "IN_STOCK", "reason": "Place"}),
    ],
)
def test_all_versioned_classes_use_global_version(capture_client, asset_data, kind, payload):
    tenant = asset_data[0]
    submit(capture_client, tenant, [operation(tenant)])
    payload = {
        key: str(tenant.location_id) if value == "TARGET" else value
        for key, value in payload.items()
    }
    row = submit(capture_client, tenant, [operation(tenant, operation=kind, payload=payload)])[0]
    assert row["sync_state"] == "REJECTED" and row["result"]["code"] == "SYNC_CONFLICT"
    assert row["result"]["current"]["version"] == 2


def test_assignment_unassignment_provenance_and_resolution(capture_client, asset_data):
    tenant = asset_data[0]
    assign = operation(
        tenant,
        operation="ASSIGN",
        payload={
            "assignee_type": "LOCATION",
            "assignee_id": str(tenant.location_id),
            "reason": "Assigned",
        },
    )
    unassign = operation(
        tenant, operation="UNASSIGN", expected_version=2, payload={"reason": "Ended"}
    )
    rows = submit(capture_client, tenant, [assign, unassign])
    assert [row["sync_state"] for row in rows] == ["APPLIED", "APPLIED"]
    assert rows[0]["result"]["client_op_id"] == assign["operation_id"]
    assert rows[1]["result"]["client_op_id"] == unassign["operation_id"]
    stale = submit(capture_client, tenant, [operation(tenant)])[0]
    conflict_id = stale["result"]["exception_id"]
    resolve = operation(
        tenant,
        operation="RESOLVE",
        entity_type="EXCEPTION",
        entity_id=conflict_id,
        expected_version=2147483647,
        payload={"expected_status": "OPEN", "note": "Operator checked"},
    )
    result = submit(capture_client, tenant, [resolve])[0]
    assert result["sync_state"] == "APPLIED" and result["result"]["to_status"] == "RESOLVED"
    exception = capture_client.get("/exceptions/" + conflict_id, headers=headers(tenant)).json()
    assert exception["status"] == "RESOLVED" and len(exception["events"]) == 1
    assert exception["resolved_by_actor_id"] == str(tenant.actor_id)
    assert submit(capture_client, tenant, [resolve])[0]["sync_state"] == "DUPLICATE"
    assert (
        submit(
            capture_client,
            tenant,
            [
                operation(
                    tenant,
                    operation="RESOLVE",
                    entity_type="EXCEPTION",
                    entity_id=conflict_id,
                    payload={"expected_status": "OPEN", "note": "Again"},
                )
            ],
        )[0]["sync_state"]
        == "REJECTED"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"actor_id": "OTHER"},
        {"entity_id": "OTHER_ASSET"},
        {"payload": {"to_location_id": "OTHER_LOCATION", "reason": "Cross tenant"}},
        {"payload": {"to_location_id": None, "reason": "Forged", "actor_id": "OTHER"}},
        {
            "payload": {
                "to_location_id": None,
                "reason": "Forged",
                "recorded_at": "1970-01-01T00:00:00Z",
            }
        },
        {"payload": {"to_location_id": None, "reason": "Forged", "expected_version": 1}},
    ],
)
def test_claims_cannot_select_authority(capture_client, asset_data, change, migrator_connection):
    tenant, other = asset_data
    mapping = {
        "OTHER": str(other.actor_id),
        "OTHER_ASSET": str(other.asset_id),
        "OTHER_LOCATION": str(other.location_id),
    }

    def convert(value):
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        return mapping.get(value, value) if isinstance(value, str) else value

    row = submit(capture_client, tenant, [operation(tenant, **convert(change))])[0]
    assert row["sync_state"] == "REJECTED"
    assert (
        migrator_connection.execute(
            select(db.assets.c.version).where(db.assets.c.id == tenant.asset_id)
        ).scalar_one()
        == 1
    )
    assert str(other.org_id) not in str(row)
    persisted = (
        migrator_connection.execute(select(db.capture_operations.c.actor_id)).scalars().all()
    )
    assert persisted == [tenant.actor_id]


def test_same_id_with_changed_payload_returns_original_without_writes(
    capture_client, asset_data, migrator_connection
):
    tenant = asset_data[0]
    op = operation(tenant)
    original = submit(capture_client, tenant, [op])[0]
    before = migrator_connection.execute(
        text("SELECT last_seq,xmin::text FROM fleetops.capture_streams")
    ).one()
    repeated = submit(
        capture_client, tenant, [dict(op, payload={"nonsense": "changed"}, client_seq=999)]
    )[0]
    after = migrator_connection.execute(
        text("SELECT last_seq,xmin::text FROM fleetops.capture_streams")
    ).one()
    assert repeated["sync_state"] == "DUPLICATE" and repeated["result"] == original["result"]
    assert before == after


def test_duplicate_under_other_tenant_cannot_read_or_execute(
    capture_client, asset_data, migrator_connection
):
    tenant, other = asset_data
    op = operation(tenant)
    submit(capture_client, tenant, [op])
    attempted = operation(other, operation_id=op["operation_id"])
    row = submit(capture_client, other, [attempted])[0]
    assert row["sync_state"] == "REJECTED" and row["result"] == {"code": "OPERATION_UNAVAILABLE"}
    assert (
        migrator_connection.execute(
            text("SELECT count(*) FROM fleetops.capture_operations")
        ).scalar_one()
        == 1
    )
    assert (
        migrator_connection.execute(
            select(db.assets.c.version).where(db.assets.c.id == other.asset_id)
        ).scalar_one()
        == 1
    )


@pytest.mark.parametrize("credentials", [{}, {"Authorization": "Bearer invalid"}])
def test_batch_requires_current_credential(
    capture_client, asset_data, credentials, migrator_connection
):
    response = capture_client.post(
        "/capture/operations", headers=credentials, json={"operations": [operation(asset_data[0])]}
    )
    assert response.status_code == 401
    assert migrator_connection.execute(select(db.capture_operations)).all() == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_seq", 0),
        ("client_seq", True),
        ("client_seq", 9223372036854775808),
        ("expected_version", 0),
        ("expected_version", True),
        ("operation", "CONSUME"),
        ("occurred_at", "1970-01-01"),
        ("recorded_at", "1970-01-01T00:00:00Z"),
        ("org_id", "tenant"),
    ],
)
def test_invalid_envelope_rejects_before_writes(
    capture_client, asset_data, field, value, migrator_connection
):
    tenant = asset_data[0]
    response = capture_client.post(
        "/capture/operations",
        headers=headers(tenant),
        json={"operations": [operation(tenant, **{field: value})]},
    )
    assert response.status_code == 422
    assert migrator_connection.execute(select(db.capture_operations)).all() == []


@pytest.mark.parametrize(
    "target",
    ["asset_id", "identifier_id", "location_id", "item_id", "vendor_id", "actor_id", "facility_id"],
)
def test_resolve_opaque_ids_is_tenant_scoped(capture_client, asset_data, target):
    tenant, other = asset_data
    response = capture_client.get(f"/resolve/{getattr(tenant, target)}", headers=headers(tenant))
    assert response.status_code == 200, response.text
    assert response.json()["entity_id"] == str(getattr(tenant, target))
    assert (
        capture_client.get(
            f"/resolve/{getattr(other, target)}", headers=headers(tenant)
        ).status_code
        == 404
    )
    assert "token" not in response.text and "storage_key" not in response.text


def test_resolve_unknown_and_session_is_safe(capture_client, asset_data, migrator_connection):
    tenant = asset_data[0]
    session = migrator_connection.execute(
        text("SELECT id FROM fleetops.sessions WHERE org_id=:org LIMIT 1"), {"org": tenant.org_id}
    ).scalar_one()
    for identifier in (uuid4(), session):
        assert (
            capture_client.get(f"/resolve/{identifier}", headers=headers(tenant)).status_code == 404
        )
    assert capture_client.get("/resolve/SERIAL-001", headers=headers(tenant)).status_code == 422
