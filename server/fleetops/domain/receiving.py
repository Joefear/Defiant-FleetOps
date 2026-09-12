"""Receiving workflow, classification and receipt-local reconciliation (ADR-010/011/012).

Python derives Exceptions. PostgreSQL independently validates typed relationships,
immutable observations and required completeness; only normal Asset initialization
crosses the narrow history/projection privilege boundary.
"""

from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import Connection, func, select, text
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.db.metadata import (
    asset_identifiers,
    items,
    parties,
    receipt_comparators,
    receipt_lines,
    receipt_reconciliations,
    receipts,
    receiving_exceptions,
)
from fleetops.db.metadata import purchase_order_lines as po_lines


class ReceivingNotFound(Exception):
    """Missing and invisible tenant targets have one public result."""


class ReceivingConflict(Exception):
    """A stale comparator, duplicate normal identifier or closed population requires retry."""


class ReceivingInvalid(Exception):
    """The supplied observations or relationships cannot satisfy the receiving contract."""


@contextmanager
def constraints():
    """Translate known database failures while preserving rollback of the whole transaction."""
    try:
        yield
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", None)
        if state in {"40001", "40P01", "23505"}:
            raise ReceivingConflict(
                "Receiving conflicts with current authoritative records"
            ) from error
        if state in {"23502", "23503", "23514", "42501", "22003", "22008"}:
            raise ReceivingInvalid("Invalid receiving observation or relationship") from error
        raise


def _header(connection: Connection, receipt_id: UUID):
    row = (
        connection.execute(select(receipts).where(receipts.c.id == receipt_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ReceivingNotFound("Receipt not found")
    return dict(row)


def _lock(connection: Connection, receipt_id: UUID):
    """Acquire the same PO-then-receipt lock order used by every durable write guard."""
    _header(connection, receipt_id)
    return dict(
        connection.execute(
            text(
                "SELECT * FROM fleetops.lock_receiving_context("
                "NULLIF(current_setting('fleetops.org_id', true),'')::uuid, :receipt)"
            ),
            {"receipt": receipt_id},
        )
        .mappings()
        .one()
    )


def _completed(connection: Connection, receipt_id: UUID) -> bool:
    return (
        connection.execute(
            select(receipt_reconciliations.c.id).where(
                receipt_reconciliations.c.receipt_id == receipt_id
            )
        ).scalar_one_or_none()
        is not None
    )


def _require_open(connection: Connection, receipt_id: UUID):
    if _completed(connection, receipt_id):
        raise ReceivingConflict("Receipt has already been reconciled")


def _comparator(connection: Connection, line_id: UUID):
    row = (
        connection.execute(select(po_lines).where(po_lines.c.id == line_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ReceivingInvalid("Invalid receipt comparator")
    return row


def _bind_comparator(connection: Connection, header, line_id: UUID):
    """Preserve an exact selected version, including an expectation with no physical arrival."""
    if header["po_id"] is None:
        raise ReceivingInvalid("Comparator requires a receipt PO")
    exists = connection.execute(
        select(receipt_comparators.c.id).where(
            receipt_comparators.c.receipt_id == header["id"],
            receipt_comparators.c.po_line_id == line_id,
        )
    ).scalar_one_or_none()
    if exists is None:
        connection.execute(
            receipt_comparators.insert().values(
                id=uuid7(),
                org_id=header["org_id"],
                receipt_id=header["id"],
                po_line_id=line_id,
            )
        )
    # Insertion of each physical line revalidates active-leaf admission even when
    # its comparator was bound in an earlier transaction. No cached leaf is authority.


def _exception(
    connection: Connection,
    header,
    kind: str,
    *,
    line=None,
    po_line_id=None,
    conflicting_asset_id=None,
):
    """Persist one derived classification using the exact ADR-012 relationship matrix."""
    values = {
        "id": uuid7(),
        "org_id": header["org_id"],
        "receipt_id": header["id"],
        "exception_type": kind,
        "receipt_line_id": None,
        "po_line_id": po_line_id,
        "asset_id": None,
        "conflicting_asset_id": conflicting_asset_id,
    }
    if line is not None:
        values.update(receipt_line_id=line["id"], po_line_id=line["po_line_id"])
        if kind not in {"QUANTITY_VARIANCE", "SERIAL_MISMATCH"}:
            values["asset_id"] = line["asset_id"]
    connection.execute(receiving_exceptions.insert().values(values))


def _line_exceptions(
    connection: Connection, header, line, *, unreadable: bool, conflicting_asset_id: UUID | None
):
    """Distinct observed facts produce distinct types; one condition cannot produce two."""
    kinds = []
    if line["po_line_id"] is None:
        kinds.append("UNEXPECTED_ITEM")
    else:
        comparator = _comparator(connection, line["po_line_id"])
        if line["item_id"] != comparator["item_id"]:
            kinds.append("SUBSTITUTION")
        if line["uom"] != comparator["uom"]:
            kinds.append("UOM_MISMATCH")
    if line["condition"] in {"DAMAGED", "OPENED"}:
        kinds.append(line["condition"])
    if line["packing_quantity"] is not None and line["quantity"] != line["packing_quantity"]:
        kinds.append("QUANTITY_VARIANCE")
    if unreadable:
        kinds.append("SERIAL_UNREADABLE")
    if conflicting_asset_id is not None:
        kinds.append("SERIAL_MISMATCH")
    for kind in kinds:
        _exception(
            connection,
            header,
            kind,
            line=line,
            conflicting_asset_id=conflicting_asset_id if kind == "SERIAL_MISMATCH" else None,
        )


def _record_line(connection: Connection, header, values: dict):
    """Select known conflict before normal admission; never recover a failed normal insert."""
    if values["po_line_id"] is not None:
        _bind_comparator(connection, header, values["po_line_id"])
    item = (
        connection.execute(select(items).where(items.c.id == values["item_id"]))
        .mappings()
        .one_or_none()
    )
    if item is None:
        raise ReceivingInvalid("Received Item not found")
    unit = values["unit"]
    if bool(item["serialized"]) != (unit is not None):
        raise ReceivingInvalid("Serialized Items require exactly one unit observation")
    # ADR-012 Decision 20 applies before either normal or known-conflict admission.
    # Reject aggregate/fractional units without rewriting their captured quantity or UOM.
    if item["serialized"] and values["quantity"] != 1:
        raise ReceivingInvalid("Serialized receipt lines require quantity exactly 1")
    fields = {key: value for key, value in values.items() if key != "unit"}
    fields.update(id=uuid7(), org_id=header["org_id"], receipt_id=header["id"])
    conflict, unreadable = None, False
    if unit is not None:
        # Owner and custodian remain explicit same-tenant business facts on both
        # paths. A conflict does not turn the existing Asset's owner into a default.
        for key in ("owner_party_id", "custodian_party_id"):
            if (
                unit[key] is not None
                and connection.execute(
                    select(parties.c.id).where(parties.c.id == unit[key])
                ).scalar_one_or_none()
                is None
            ):
                raise ReceivingInvalid("Invalid received-unit Party")
        identifier = unit["identifier"]
        fields.update(
            owner_party_id=unit["owner_party_id"], custodian_party_id=unit["custodian_party_id"]
        )
        if identifier["value"] is not None:
            conflict = connection.execute(
                select(asset_identifiers.c.asset_id).where(
                    asset_identifiers.c.type == identifier["type"],
                    asset_identifiers.c.value == identifier["value"],
                )
            ).scalar_one_or_none()
        if conflict is not None:
            fields.update(
                observed_identifier_type=identifier["type"],
                observed_identifier_value=identifier["value"],
            )
        else:
            params = fields | {
                "asset_id": uuid7(),
                "asset_tag": unit["asset_tag"],
                "description": unit["description"],
                "identifier_id": uuid7(),
                "identifier_type": identifier["type"],
                "identifier_value": identifier["value"],
                "unreadable_reason": identifier["unreadable_reason"],
                "transition_id": uuid7(),
            }
            line = dict(
                connection.execute(
                    text("""
                SELECT * FROM fleetops.create_received_unit(
                    :receipt_id,:id,:po_line_id,:item_id,:quantity,:uom,:condition,
                    :packing_quantity,:notes,:asset_id,:asset_tag,:description,
                    :owner_party_id,:custodian_party_id,:identifier_id,:identifier_type,
                    :identifier_value,:unreadable_reason,:transition_id)
            """),
                    params,
                )
                .mappings()
                .one()
            )
            unreadable = identifier["value"] is None
    if unit is None or conflict is not None:
        line = dict(
            connection.execute(receipt_lines.insert().values(fields).returning(receipt_lines))
            .mappings()
            .one()
        )
    _line_exceptions(connection, header, line, unreadable=unreadable, conflicting_asset_id=conflict)
    return line


def _reconcile(connection: Connection, header):
    """Close one receipt population once; committed quantity Exceptions never need edits."""
    _require_open(connection, header["id"])
    connection.execute(
        receipt_reconciliations.insert().values(
            id=uuid7(),
            org_id=header["org_id"],
            receipt_id=header["id"],
        )
    )
    targets = (
        connection.execute(
            select(po_lines)
            .join(
                receipt_comparators,
                (receipt_comparators.c.org_id == po_lines.c.org_id)
                & (receipt_comparators.c.po_line_id == po_lines.c.id),
            )
            .where(receipt_comparators.c.receipt_id == header["id"])
        )
        .mappings()
        .all()
    )
    for comparator in targets:
        # PostgreSQL NUMERIC retains the raw observations' arbitrary precision.
        # Python's default Decimal context would round a large receipt-local sum.
        total, mismatched = connection.execute(
            select(
                func.coalesce(
                    func.sum(receipt_lines.c.quantity).filter(
                        receipt_lines.c.uom == comparator["uom"]
                    ),
                    0,
                ),
                func.coalesce(func.bool_or(receipt_lines.c.uom != comparator["uom"]), False),
            ).where(
                receipt_lines.c.receipt_id == header["id"],
                receipt_lines.c.po_line_id == comparator["id"],
            )
        ).one()
        if mismatched:
            continue
        if total != comparator["quantity"]:
            _exception(
                connection,
                header,
                "SHORT" if total < comparator["quantity"] else "OVER",
                po_line_id=comparator["id"],
            )


def _validate_complete(connection: Connection):
    """Surface deferred invariant failures inside the service error/rollback boundary."""
    connection.exec_driver_sql("SET CONSTRAINTS fleetops.receiving_40_complete IMMEDIATE")
    connection.exec_driver_sql("SET CONSTRAINTS fleetops.receiving_40_complete DEFERRED")


def create_receipt(connection: Connection, *, org_id: UUID, values: dict):
    """Capture a full delivery atomically or establish an immutable header for incremental entry."""
    with constraints():
        header_values = {
            key: value
            for key, value in values.items()
            if key not in {"lines", "comparator_ids", "reconcile"}
        }
        header = dict(
            connection.execute(
                receipts.insert()
                .values(
                    **header_values,
                    id=uuid7(),
                    org_id=org_id,
                )
                .returning(receipts)
            )
            .mappings()
            .one()
        )
        for target in values["comparator_ids"]:
            _bind_comparator(connection, header, target)
        for line in values["lines"]:
            _record_line(connection, header, line)
        if values["reconcile"]:
            _reconcile(connection, header)
        _validate_complete(connection)
        return get_receipt(connection, header["id"])


def add_line(connection: Connection, receipt_id: UUID, *, values: dict):
    """Append one immutable observation and its complete unit consequences in one transaction."""
    with constraints():
        header = _lock(connection, receipt_id)
        _require_open(connection, receipt_id)
        _record_line(connection, header, values)
        _validate_complete(connection)
        return get_receipt(connection, receipt_id)


def reconcile_receipt(connection: Connection, receipt_id: UUID):
    """Record receipt-local quantity truth after capture, with no cross-receipt state."""
    with constraints():
        header = _lock(connection, receipt_id)
        _reconcile(connection, header)
        _validate_complete(connection)
        return get_receipt(connection, receipt_id)


def get_receipt(connection: Connection, receipt_id: UUID):
    """Read stored physical facts and derived completion through tenant RLS."""
    header = _header(connection, receipt_id)
    header["reconciled"] = _completed(connection, receipt_id)
    header["comparator_ids"] = list(
        connection.execute(
            select(receipt_comparators.c.po_line_id)
            .where(receipt_comparators.c.receipt_id == receipt_id)
            .order_by(receipt_comparators.c.id)
        ).scalars()
    )
    for key, table in (("lines", receipt_lines), ("exceptions", receiving_exceptions)):
        header[key] = (
            connection.execute(
                select(table).where(table.c.receipt_id == receipt_id).order_by(table.c.id)
            )
            .mappings()
            .all()
        )
    return header


def list_receipts(connection: Connection):
    """Return only the authenticated organization's receiving history."""
    return [
        get_receipt(connection, receipt_id)
        for receipt_id in connection.execute(
            select(receipts.c.id).order_by(receipts.c.id)
        ).scalars()
    ]
