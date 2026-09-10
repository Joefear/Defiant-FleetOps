"""ADR-007 first-change witnesses, immutable reconstruction and global reconciliation."""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from server.tests.auth_context import set_authenticated
from server.tests.slice5.conftest import headers, runtime_transition
from server.tests.slice6.conftest import (
    CUSTODY,
    KINDS,
    MOVE,
    OWNERSHIP,
    baseline_values,
    for_asset,
    history_values,
    runtime_fact,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import asset_initial_facts, asset_movements, assets, locations
from fleetops.domain.asset_facts import reconcile_assets


def health(connection, tenant):
    """Use ordinary runtime RLS for reconciliation, without privileged repair."""
    with connection.begin():
        set_authenticated(connection, tenant)
        return reconcile_assets(connection)


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_five_changes_reconstruct_from_baseline_and_preserve_orthogonal_facts(
    kind,
    asset_client,
    asset_data,
    fact_snapshot,
):
    """The five-movement handoff proof also exercises equivalent custody/ownership histories."""
    a = asset_data[0]
    before = fact_snapshot(a)
    initial = before["initial"][kind.initial]
    targets = (
        [a.other_location_id, None, a.location_id, a.other_location_id, a.location_id]
        if kind is MOVE
        else [a.other_manufacturer_id, None, a.vendor_id, a.party_id, a.other_manufacturer_id]
        if kind is CUSTODY
        else [a.other_manufacturer_id, a.party_id, a.vendor_id, a.party_id, a.other_manufacturer_id]
    )
    times = [datetime(year, 1, 1, tzinfo=UTC) for year in (2099, 1970, 2050, 2000, 1980)]
    prior = initial
    returned = []
    for expected, (target, occurred) in enumerate(zip(targets, times, strict=True), 1):
        response = asset_client.post(
            f"/assets/{a.asset_id}/{kind.route}",
            headers=headers(a),
            json=dict(
                expected_version=expected,
                **{kind.to_field: str(target) if target else None},
                reason="Reconstruction",
                occurred_at=occurred.isoformat(),
            ),
        )
        assert response.status_code == 201, response.text
        row = response.json()
        assert row[kind.from_field] == (str(prior) if prior else None)
        assert row[kind.to_field] == (str(target) if target else None)
        assert row["result_version"] == expected + 1
        assert row["actor_id"] == str(a.actor_id)
        assert datetime.fromisoformat(row["occurred_at"]) == occurred
        assert datetime.fromisoformat(row["recorded_at"]).tzinfo is not None
        assert row[kind.correction] is row["client_op_id"] is None
        assert UUID(row["id"]).version == 7
        returned.append(row)
        after = fact_snapshot(a)
        assert after["initial"] == before["initial"]
        assert after["asset"][kind.projection] == target
        assert after["asset"]["version"] == expected + 1
        assert {
            k: v for k, v in after["asset"].items() if k not in {kind.projection, "version"}
        } == {k: v for k, v in before["asset"].items() if k not in {kind.projection, "version"}}
        assert after["asset_transitions"] == before["asset_transitions"]
        for other in KINDS:
            if other is not kind:
                assert after[other.table.name] == []
        prior = target
    response = asset_client.get(f"/assets/{a.asset_id}/{kind.route}", headers=headers(a))
    assert response.status_code == 200
    assert response.json() == returned
    assert [row["result_version"] for row in response.json()] == [2, 3, 4, 5, 6]
    assert [initial, *(row[kind.to_field] for row in fact_snapshot(a)[kind.table.name])] == [
        initial,
        *targets,
    ]
    assert asset_client.get("/health/assets/reconciliation", headers=headers(a)).json() == []


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize("later", [False, True], ids=["before-first", "after-history"])
def test_projection_corruption_rejects_without_writes_or_repair(
    kind,
    later,
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    # Three distinct facts reproduce D3's A -> corrupted B -> requested C scenario.
    # Rejection must not depend on the request merely repeating the corrupted value.
    target = uuid7() if kind is MOVE else a.vendor_id if kind is CUSTODY else a.party_id
    if kind is MOVE:
        migrator_connection.execute(
            locations.insert().values(
                id=target,
                org_id=a.org_id,
                facility_id=a.facility_id,
                code=str(target),
                name="Third destination",
                kind="BIN",
                created_by_actor_id=a.actor_id,
            )
        )
        migrator_connection.commit()
    expected = 1
    if later:
        runtime_fact(app_connection, a, kind)
        expected = 2
    corrupt = (
        (a.location_id if kind is MOVE else a.vendor_id if kind is OWNERSHIP else a.party_id)
        if later
        else getattr(a, kind.target_attribute)
    )
    migrator_connection.execute(
        assets.update().where(assets.c.id == a.asset_id).values(**{kind.projection: corrupt})
    )
    migrator_connection.commit()
    before = fact_snapshot(a)
    authoritative_prior = (
        before[kind.table.name][-1][kind.to_field] if later else before["initial"][kind.initial]
    )
    assert len({authoritative_prior, corrupt, target}) == 3
    rows = health(app_connection, a)
    category = (
        f"{kind.name}_projection_mismatch"
        if later
        else f"initial_{'location' if kind is MOVE else kind.name}_projection_mismatch"
    )
    assert len(rows) == 1 and rows[0]["discrepancies"] == [category]
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, a, kind, expected_version=expected, target=target)
    assert error.value.orig.sqlstate == "P0001"
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", [MOVE, CUSTODY], ids=lambda k: k.name)
@pytest.mark.parametrize("initial_null", [False, True])
def test_first_change_uses_null_safe_baseline_equality(
    kind,
    initial_null,
    asset_data,
    seed_asset,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    value = None if initial_null else getattr(a, kind.target_attribute)
    row = seed_asset(a, **{kind.projection: value})
    tenant = for_asset(a, row["id"])
    assert health(app_connection, tenant) == []
    corrupt = getattr(a, kind.target_attribute) if initial_null else None
    migrator_connection.execute(
        assets.update().where(assets.c.id == tenant.asset_id).values(**{kind.projection: corrupt})
    )
    migrator_connection.commit()
    before = fact_snapshot(tenant)
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, tenant, kind)
    assert error.value.orig.sqlstate == "P0001"
    assert fact_snapshot(tenant) == before
    assert health(app_connection, tenant)[0]["asset_id"] == tenant.asset_id
    # Privileged restoration returns to the original evidence, not a lazy baseline repair.
    migrator_connection.execute(
        assets.update().where(assets.c.id == tenant.asset_id).values(**{kind.projection: value})
    )
    migrator_connection.commit()
    result = runtime_fact(app_connection, tenant, kind, target=None)
    assert result[kind.from_field] == value and result[kind.to_field] is None
    assert health(app_connection, tenant) == []


def test_missing_baseline_rejects_all_first_changes_without_lazy_creation(
    asset_data,
    seed_asset,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    tenant = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    before = fact_snapshot(tenant)
    assert before["initial"] is None
    for kind in KINDS:
        with pytest.raises(DBAPIError) as error:
            runtime_fact(app_connection, tenant, kind)
        assert error.value.orig.sqlstate == "P0001"
        assert error.value.orig.diag.message_primary == "Asset initial facts missing"
        assert fact_snapshot(tenant) == before
    rows = health(app_connection, tenant)
    assert len(rows) == 1 and rows[0]["discrepancies"] == ["missing_initial_facts"]


def test_baseline_insertion_has_no_version_or_change_history_effect(
    asset_data,
    seed_asset,
    migrator_connection,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    tenant = for_asset(a, seed_asset(a, initial_facts=False)["id"])
    before = fact_snapshot(tenant)
    # Explicit fixture creation inputs, not a query of current projections.
    migrator_connection.execute(asset_initial_facts.insert().values(baseline_values(tenant)))
    migrator_connection.commit()
    after = fact_snapshot(tenant)
    assert after["asset"] == before["asset"]
    assert after["asset"]["version"] == 1
    assert "result_version" not in asset_initial_facts.c
    assert after["asset_transitions"] == before["asset_transitions"]
    assert [row["result_version"] for row in after["asset_transitions"]] == [1]
    assert all(after[kind.table.name] == [] for kind in KINDS)
    assert health(app_connection, tenant) == []


def mixed_sequence(connection, tenant):
    """Every global version is produced legitimately, with deliberate per-class gaps."""
    runtime_fact(connection, tenant, MOVE)
    runtime_fact(connection, tenant, CUSTODY, expected_version=2)
    runtime_fact(connection, tenant, OWNERSHIP, expected_version=3)
    runtime_transition(connection, tenant, expected_version=4)
    runtime_fact(connection, tenant, MOVE, expected_version=5, target=tenant.location_id)


def test_combined_complete_sequence_excludes_baseline_and_preserves_creation(
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
    asset_client,
):
    a = asset_data[0]
    original = fact_snapshot(a)["initial"]
    mixed_sequence(app_connection, a)
    after = fact_snapshot(a)
    assert after["asset"]["version"] == 6 and after["initial"] == original
    assert {
        name: [h["result_version"] for h in after[name]]
        for name in (
            "asset_transitions",
            "asset_movements",
            "asset_custody_changes",
            "asset_ownership_changes",
        )
    } == {
        "asset_transitions": [1, 5],
        "asset_movements": [2, 6],
        "asset_custody_changes": [3],
        "asset_ownership_changes": [4],
    }
    with app_connection.begin():
        set_authenticated(app_connection, a)
        versions = (
            app_connection.execute(
                text("""
            SELECT result_version FROM (
                SELECT asset_id, result_version FROM fleetops.asset_transitions
                UNION ALL SELECT asset_id, result_version FROM fleetops.asset_movements
                UNION ALL SELECT asset_id, result_version FROM fleetops.asset_custody_changes
                UNION ALL SELECT asset_id, result_version FROM fleetops.asset_ownership_changes
            ) h WHERE asset_id = :asset ORDER BY result_version
        """),
                {"asset": a.asset_id},
            )
            .scalars()
            .all()
        )
        assert versions == [1, 2, 3, 4, 5, 6]
        assert reconcile_assets(app_connection) == []
    response = asset_client.patch(
        f"/assets/{a.asset_id}", headers=headers(a), json={"description": "Descriptive edit"}
    )
    assert response.status_code == 200
    assert fact_snapshot(a)["initial"] == original
    assert health(app_connection, a) == []
    # Missing creation truth remains visible even when all classes have later history.
    migrator_connection.execute(
        asset_initial_facts.delete().where(asset_initial_facts.c.asset_id == a.asset_id)
    )
    migrator_connection.commit()
    assert health(app_connection, a)[0]["discrepancies"] == ["missing_initial_facts"]


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "ahead"])
def test_combined_global_corruption_is_reported_without_repair(
    corruption,
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    mixed_sequence(app_connection, a)
    if corruption == "missing":
        migrator_connection.execute(
            CUSTODY.table.delete().where(CUSTODY.table.c.asset_id == a.asset_id)
        )
    elif corruption == "duplicate":
        migrator_connection.execute(
            asset_movements.insert().values(history_values(a, MOVE, result_version=3))
        )
    else:
        migrator_connection.execute(
            CUSTODY.table.insert().values(history_values(a, CUSTODY, result_version=9))
        )
    migrator_connection.commit()
    before = fact_snapshot(a)
    row = health(app_connection, a)[0]
    category, field, expected = {
        "missing": ("global_version_missing", "global_missing_version_ranges", [[3, 3]]),
        "duplicate": ("global_version_duplicate", "global_duplicate_versions", [3]),
        "ahead": ("global_history_version_ahead", "global_ahead_versions", [9]),
    }[corruption]
    assert category in row["discrepancies"] and row[field] == expected
    assert fact_snapshot(a) == before


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_class_history_ahead_rejects_and_reports_impossible_direction(
    kind,
    asset_data,
    app_connection,
    migrator_connection,
    fact_snapshot,
):
    a = asset_data[0]
    # Keep the projection equal to latest history so only the direction check can reject.
    current = fact_snapshot(a)["asset"][kind.projection]
    migrator_connection.execute(
        kind.table.insert().values(
            history_values(a, kind, result_version=8, **{kind.to_field: current})
        )
    )
    migrator_connection.commit()
    before = fact_snapshot(a)
    categories = health(app_connection, a)[0]["discrepancies"]
    assert f"{kind.name}_history_version_ahead" in categories
    assert "global_history_version_ahead" in categories
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, a, kind)
    assert error.value.orig.sqlstate == "P0001"
    assert fact_snapshot(a) == before


def test_large_missing_global_interval_is_bounded_without_generating_versions(
    asset_data,
    app_connection,
    migrator_connection,
):
    a = asset_data[0]
    migrator_connection.execute(
        assets.update().where(assets.c.id == a.asset_id).values(version=2147483647)
    )
    migrator_connection.commit()
    with app_connection.begin():
        set_authenticated(app_connection, a)
        app_connection.exec_driver_sql("SET LOCAL statement_timeout = '5s'")
        row = reconcile_assets(app_connection)[0]
    assert row["discrepancies"] == ["global_version_missing"]
    assert row["global_missing_version_ranges"] == [[2, 2147483647]]


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
def test_client_operation_seam_is_correlation_without_apply_once(
    kind,
    asset_data,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    correlation = uuid7()
    first = runtime_fact(app_connection, a, kind, client_op_id=correlation)
    second = runtime_fact(app_connection, a, kind, expected_version=2, client_op_id=correlation)
    assert first["client_op_id"] == second["client_op_id"] == correlation
    after = fact_snapshot(a)
    assert after["asset"]["version"] == 3
    assert len(after[kind.table.name]) == 2


@pytest.mark.parametrize("kind", KINDS, ids=lambda k: k.name)
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"expected_version": None}, "40001"),
        ({"expected_version": 0}, "40001"),
        ({"reason": None}, "23502"),
        ({"reason": "  \t\n"}, "23514"),
        ({"reason": "x" * 4001}, "23514"),
        ({"occurred_at": None}, "23502"),
        ({"history_id": None}, "23502"),
    ],
)
def test_failed_function_has_no_partial_writes(
    kind,
    changes,
    code,
    asset_data,
    app_connection,
    fact_snapshot,
):
    a = asset_data[0]
    before = fact_snapshot(a)
    with pytest.raises(DBAPIError) as error:
        runtime_fact(app_connection, a, kind, **changes)
    assert error.value.orig.sqlstate == code
    assert fact_snapshot(a) == before
