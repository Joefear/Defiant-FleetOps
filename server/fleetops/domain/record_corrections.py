"""Typed procurement/receipt corrections and synchronous Exception consequences."""

from uuid import UUID

from sqlalchemy import Connection, select, text
from uuid6 import uuid7

from fleetops.db.metadata import (
    exception_workflows,
    purchase_order_line_corrections,
    purchase_order_lines,
    receipt_comparators,
    receipt_correction_evaluations,
    receipt_evaluation_exceptions,
    receipt_evaluation_expectations,
    receipt_evaluation_lines,
    receipt_line_corrections,
    receipt_lines,
    receiving_exceptions,
)
from fleetops.domain import procurement, receiving
from fleetops.domain.assets import AssetConflict, AssetInvalid, AssetNotFound, _constraints

PO_FIELDS = ("item_id", "quantity", "uom", "unit_price", "expected_date")
LINE_FIELDS = (
    "po_line_id",
    "item_id",
    "quantity",
    "uom",
    "condition",
    "packing_quantity",
    "notes",
    "serialized",
    "owner_party_id",
    "custodian_party_id",
    "asset_id",
    "observed_identifier_type",
    "observed_identifier_value",
)


def _head(connection, table, root_column, root_id, original):
    """Reject malformed committed history before deriving a deterministic head."""
    # Both identifiers are fixed module-owned SQLAlchemy mappings, never request text.
    bad = connection.execute(
        text(f"SELECT 1 FROM fleetops.{table.name}_anomalies WHERE root_id=:root"),
        {"root": root_id},
    ).first()
    if bad:
        raise AssetConflict("Malformed correction authority")
    latest = (
        connection.execute(
            select(table)
            .where(table.c[root_column] == root_id, table.c.correction_role == "CORRECTED")
            .order_by(table.c.correction_generation.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return dict(latest) if latest else dict(original, correction_generation=0)


def _pair(connection, table, root_column, original, head, fields, values, entity):
    """Materialize full payloads from the locked effective head, retaining previous fixes."""
    expected = values["expected_generation"]
    if expected != head["correction_generation"]:
        raise AssetConflict("Correction generation is stale")
    replacement = {field: head[field] for field in fields}
    replacement.update({key: value for key, value in values.items() if key in fields})
    common = dict(
        org_id=original["org_id"],
        **{root_column: original["id"], entity: original[entity]},
        correction_pair_id=uuid7(),
        correction_generation=expected + 1,
        correction_occurred_at=values["correction_occurred_at"],
        reason=values["reason"],
        occurred_at=head.get("occurred_at", original.get("created_at")),
    )
    with _constraints():
        for role, payload in (
            ("REVERSAL", {field: head[field] for field in fields}),
            ("CORRECTED", replacement),
        ):
            result = (
                connection.execute(
                    table.insert()
                    .values(**common, **payload, id=uuid7(), correction_role=role)
                    .returning(table)
                )
                .mappings()
                .one()
            )
    return dict(result)


def correct_procurement(connection: Connection, po_id: UUID, root_id: UUID, *, values: dict):
    """Correct unreferenced issued business facts under the same PO lock as receipt binding."""
    order = procurement.get_order(connection, po_id, lock=True)
    if order["status"] != "ISSUED":
        raise AssetInvalid("Only issued procurement lines are correctable")
    original = (
        connection.execute(
            select(purchase_order_lines).where(
                purchase_order_lines.c.id == root_id, purchase_order_lines.c.po_id == po_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if original is None:
        raise AssetNotFound("Procurement line not found")
    if connection.execute(
        select(receipt_comparators.c.id).where(receipt_comparators.c.po_line_id == root_id)
    ).first():
        raise AssetConflict("Receipt-bound procurement facts are frozen")
    head = _head(connection, purchase_order_line_corrections, "po_line_id", root_id, original)
    return _pair(
        connection,
        purchase_order_line_corrections,
        "po_line_id",
        original,
        head,
        PO_FIELDS,
        values,
        "po_id",
    )


def _key(row):
    """A changed typed relationship is a distinct disagreement, even with the same type."""
    return tuple(
        row[field]
        for field in (
            "exception_type",
            "receipt_line_id",
            "po_line_id",
            "asset_id",
            "conflicting_asset_id",
        )
    )


def _disagreements(connection, evaluation):
    """Python classifies; PostgreSQL NUMERIC alone accumulates quantities without rounding."""
    params = {"evaluation": evaluation["id"]}
    lines = (
        connection.execute(
            text("SELECT * FROM fleetops.evaluated_receipt_lines WHERE evaluation_id=:evaluation"),
            params,
        )
        .mappings()
        .all()
    )
    comparators = {
        row["po_line_id"]: row
        for row in connection.execute(
            text(
                "SELECT * FROM fleetops.evaluated_receipt_expectations WHERE "
                "evaluation_id=:evaluation"
            ),
            params,
        ).mappings()
    }
    result = []
    for line in lines:
        kinds = []
        comparator = comparators.get(line["po_line_id"])
        if line["po_line_id"] is None:
            kinds.append("UNEXPECTED_ITEM")
        elif comparator is None:
            raise AssetConflict("Receipt comparator source missing")
        else:
            if line["item_id"] != comparator["item_id"]:
                kinds.append("SUBSTITUTION")
            if line["uom"] != comparator["uom"]:
                kinds.append("UOM_MISMATCH")
        if line["condition"] in ("DAMAGED", "OPENED"):
            kinds.append(line["condition"])
        if line["packing_quantity"] is not None and line["packing_quantity"] != line["quantity"]:
            kinds.append("QUANTITY_VARIANCE")
        if line["unreadable"]:
            kinds.append("SERIAL_UNREADABLE")
        if line["conflicting_asset_id"] is not None:
            kinds.append("SERIAL_MISMATCH")
        for kind in kinds:
            result.append(
                dict(
                    exception_type=kind,
                    receipt_line_id=line["receipt_line_id"],
                    po_line_id=line["po_line_id"],
                    asset_id=None
                    if kind in ("QUANTITY_VARIANCE", "SERIAL_MISMATCH")
                    else line["asset_id"],
                    conflicting_asset_id=line["conflicting_asset_id"]
                    if kind == "SERIAL_MISMATCH"
                    else None,
                )
            )
    for comparator in comparators.values():
        total, mismatch = connection.execute(
            text("""
          SELECT COALESCE(sum(quantity),0),COALESCE(bool_or(uom<>:uom),false)
          FROM fleetops.evaluated_receipt_lines WHERE evaluation_id=:evaluation AND
            po_line_id=:po_line
        """),
            dict(params, uom=comparator["uom"], po_line=comparator["po_line_id"]),
        ).one()
        if not mismatch and total != comparator["quantity"]:
            result.append(
                dict(
                    exception_type="SHORT" if total < comparator["quantity"] else "OVER",
                    receipt_line_id=None,
                    po_line_id=comparator["po_line_id"],
                    asset_id=None,
                    conflicting_asset_id=None,
                )
            )
    return result


def _evaluate(connection, header, corrected):
    """Pin the whole sealed population and lock workflows in fixed identity order.

    Standalone status writes lock only their workflow. This path already holds PO
    then receipt, and never reverses that order while acquiring workflow locks.
    """
    sequence = connection.execute(
        text(
            "SELECT COALESCE(max(evaluation_seq),0)+1 FROM "
            "fleetops.receipt_correction_evaluations WHERE receipt_id=:receipt"
        ),
        {"receipt": header["id"]},
    ).scalar_one()
    evaluation = dict(
        connection.execute(
            receipt_correction_evaluations.insert()
            .values(
                id=uuid7(),
                org_id=header["org_id"],
                receipt_id=header["id"],
                receipt_line_id=corrected["receipt_line_id"],
                correction_id=corrected["id"],
                correction_generation=corrected["correction_generation"],
                evaluation_seq=sequence,
                occurred_at=corrected["correction_occurred_at"],
            )
            .returning(receipt_correction_evaluations)
        )
        .mappings()
        .one()
    )
    common = dict(
        org_id=header["org_id"],
        evaluation_id=evaluation["id"],
        occurred_at=evaluation["occurred_at"],
    )
    for original in connection.execute(
        select(receipt_lines).where(receipt_lines.c.receipt_id == header["id"])
    ).mappings():
        head = _head(
            connection, receipt_line_corrections, "receipt_line_id", original["id"], original
        )
        connection.execute(
            receipt_evaluation_lines.insert().values(
                **common,
                id=uuid7(),
                receipt_line_id=original["id"],
                source_generation=head["correction_generation"],
                source_id=head["id"] if head["correction_generation"] else None,
            )
        )
    for binding in connection.execute(
        select(receipt_comparators).where(receipt_comparators.c.receipt_id == header["id"])
    ).mappings():
        connection.execute(
            receipt_evaluation_expectations.insert().values(
                **common,
                id=uuid7(),
                po_line_id=binding["po_line_id"],
                source_generation=binding["source_generation"],
                source_id=binding["source_id"],
            )
        )
    workflows = (
        connection.execute(
            select(exception_workflows)
            .join(
                receiving_exceptions,
                receiving_exceptions.c.id == exception_workflows.c.exception_id,
            )
            .where(receiving_exceptions.c.receipt_id == header["id"])
            .order_by(exception_workflows.c.exception_id)
            .with_for_update(of=exception_workflows)
        )
        .mappings()
        .all()
    )
    workflow_by_id = {row["exception_id"]: row for row in workflows}
    desired = {_key(row): row for row in _disagreements(connection, evaluation)}
    claimed = set()
    observations = list(
        connection.execute(
            select(receiving_exceptions)
            .where(receiving_exceptions.c.receipt_id == header["id"])
            .order_by(receiving_exceptions.c.id)
        ).mappings()
    )
    for observation in observations:
        prior = connection.execute(
            text("""
          SELECT x.supported FROM fleetops.receipt_evaluation_exceptions x
          JOIN fleetops.receipt_correction_evaluations e ON e.org_id=x.org_id AND
            e.id=x.evaluation_id
          WHERE x.exception_id=:exception ORDER BY e.evaluation_seq DESC LIMIT 1
        """),
            {"exception": observation["id"]},
        ).scalar_one_or_none()
        supported = prior is not False and _key(observation) in desired
        workflow = workflow_by_id[observation["id"]]
        if supported:
            if _key(observation) in claimed:
                raise AssetConflict("Duplicate supported observation")
            claimed.add(_key(observation))
        connection.execute(
            receipt_evaluation_exceptions.insert().values(
                **common,
                id=uuid7(),
                exception_id=observation["id"],
                supported=supported,
                prior_event_seq=workflow["event_seq"],
            )
        )
        if not supported and workflow["status"] in ("OPEN", "ACKNOWLEDGED"):
            # D11/D42: identify the automatic supersession and reference the pair's
            # immutable reason. Copying/prefixing a 4,000-character reason here
            # would either omit that explanation, truncate evidence, or overflow.
            connection.execute(
                text("""
              SELECT * FROM fleetops.transition_exception(:exception,:expected,'RESOLVED',
                :note,:occurred,:event,:evaluation)
            """),
                dict(
                    exception=observation["id"],
                    expected=workflow["status"],
                    note=(
                        "Corrected receipt reality superseded this observation's assertion. "
                        "Reason: see receipt-line correction pair "
                        f"{corrected['correction_pair_id']}."
                    ),
                    occurred=evaluation["occurred_at"],
                    event=uuid7(),
                    evaluation=evaluation["id"],
                ),
            )
    for key, disagreement in desired.items():
        if key in claimed:
            continue
        identifier = uuid7()
        connection.execute(
            receiving_exceptions.insert().values(
                org_id=header["org_id"],
                evaluation_id=evaluation["id"],
                **disagreement,
                id=identifier,
                receipt_id=header["id"],
            )
        )
        connection.execute(
            receipt_evaluation_exceptions.insert().values(
                **common, id=uuid7(), exception_id=identifier, supported=True, prior_event_seq=0
            )
        )
    return evaluation


def correct_receipt_line(connection: Connection, receipt_id: UUID, root_id: UUID, *, values: dict):
    """Append a full typed replacement and all evaluation consequences in one transaction."""
    with _constraints():
        header = receiving._lock(connection, receipt_id)
        if not receiving._completed(connection, receipt_id):
            raise AssetConflict("Receipt correction requires completed capture")
        original = (
            connection.execute(
                select(receipt_lines).where(
                    receipt_lines.c.id == root_id, receipt_lines.c.receipt_id == receipt_id
                )
            )
            .mappings()
            .one_or_none()
        )
        if original is None:
            raise AssetNotFound("Receipt line not found")
        head = _head(connection, receipt_line_corrections, "receipt_line_id", root_id, original)
        corrected = _pair(
            connection,
            receipt_line_corrections,
            "receipt_line_id",
            original,
            head,
            LINE_FIELDS,
            values,
            "receipt_id",
        )
        if corrected["po_line_id"] is not None:
            receiving._bind_comparator(
                connection,
                header,
                corrected["po_line_id"],
                expected_generation=values.get("expected_po_generation", 0),
                correction_id=corrected["id"],
            )
        evaluation = _evaluate(connection, header, corrected)
        connection.execute(
            text("SELECT fleetops.assert_receipt_correction_complete(:org,:receipt,:pair)"),
            dict(org=header["org_id"], receipt=receipt_id, pair=corrected["correction_pair_id"]),
        )
        return dict(corrected, evaluation_id=evaluation["id"])


def record_history(connection, table, root_column, root_id):
    """Read every pair member in generation/role order; no timestamp chooses authority."""
    # Resolve correction helpers after asset-domain initialization: corrections
    # imports assets, whose history reader calls back into corrections.
    from fleetops.domain.corrections import audit_rows

    rows = (
        connection.execute(
            select(table)
            .where(table.c[root_column] == root_id)
            .order_by(table.c.correction_generation, table.c.correction_role.desc())
        )
        .mappings()
        .all()
    )

    return audit_rows(rows, root_column)
