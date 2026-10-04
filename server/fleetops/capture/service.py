"""Each queued operation commits its domain consequences and terminal result together."""

import hashlib
from collections import OrderedDict

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from uuid6 import uuid7

from fleetops.api.asset_schemas import MovementRequest, PhysicalChangeRequest, TransitionRequest
from fleetops.api.assignment_schemas import AssignmentRequest
from fleetops.api.capture_schemas import EvidencePayload, ReceiveScan, ResolvePayload
from fleetops.api.evidence_schemas import AttachmentLinkCreate
from fleetops.auth import resolve_identity
from fleetops.capture.types import VERSIONED
from fleetops.db import metadata as db
from fleetops.db.tenancy import set_credential_context, set_organization
from fleetops.domain import asset_facts, assignments, exception_workflow, receiving
from fleetops.domain.assets import (
    AssetConflict,
    AssetInvalid,
    AssetNotFound,
    transition_asset,
)
from fleetops.domain.lifecycle import LifecycleInvalid
from fleetops.evidence import service as evidence

MODELS = {
    "MOVE": MovementRequest,
    "ASSIGN": AssignmentRequest,
    "UNASSIGN": PhysicalChangeRequest,
    "TRANSITION": TransitionRequest,
}
FAILURES = (
    AssetInvalid,
    AssetConflict,
    AssetNotFound,
    LifecycleInvalid,
    receiving.ReceivingInvalid,
    receiving.ReceivingConflict,
    receiving.ReceivingNotFound,
    evidence.EvidenceInvalid,
    evidence.EvidenceConflict,
    evidence.EvidenceNotFound,
)


class CredentialExpired(Exception):
    """Revalidation occurs for each transaction, including duplicate requests."""


def ordered(operations):
    """Sort claims within each client epoch and keep batch response positions stable."""
    groups = OrderedDict()
    for index, operation in enumerate(operations):
        groups.setdefault((operation.client_id, operation.client_epoch), []).append(
            (index, operation)
        )
    return [
        entry
        for group in groups.values()
        for entry in sorted(group, key=lambda entry: (entry[1].client_seq, entry[0]))
    ]


def _out(row, *, duplicate=False):
    return dict(
        operation_id=row["operation_id"],
        sync_state="DUPLICATE" if duplicate else row["sync_state"],
        recorded_at=row["recorded_at"],
        result=row["result"],
        sequence_flags=row["sequence_flags"],
    )


def _sequence(connection, identity, operation):
    table = db.capture_streams
    key = dict(
        org_id=identity.organization_id,
        client_id=operation.client_id,
        client_epoch=operation.client_epoch,
    )
    connection.execute(insert(table).values(**key).on_conflict_do_nothing())
    row = (
        connection.execute(
            select(table)
            .where(*(table.c[field] == value for field, value in key.items()))
            .with_for_update()
        )
        .mappings()
        .one()
    )
    if row["actor_id"] != identity.actor_id:
        raise AssetInvalid("Use a new client epoch after changing the performing Actor")
    flags = []
    if operation.client_seq <= row["last_seq"]:
        flags.append("SEQUENCE_REUSED")
    elif operation.client_seq != row["last_seq"] + 1:
        flags.append("SEQUENCE_GAP")
    connection.execute(
        table.update()
        .where(*(table.c[field] == value for field, value in key.items()))
        .values(last_seq=max(operation.client_seq, row["last_seq"]))
    )
    return flags


def _facts(connection, asset, version):
    """Historical facts at a known version; clocks and current projections cannot rewrite it."""
    if version > asset["version"]:
        return {"version": version, "known_version": False}
    facts = {"version": version, "known_version": True}
    for table, field, name, baseline in (
        (db.asset_transitions, "to_state", "state", None),
        (db.asset_movements, "to_location_id", "location_id", "initial_location_id"),
        (
            db.asset_custody_changes,
            "to_custodian_party_id",
            "custodian_party_id",
            "initial_custodian_party_id",
        ),
        (
            db.asset_ownership_changes,
            "to_owner_party_id",
            "owner_party_id",
            "initial_owner_party_id",
        ),
        (db.asset_assignment_events, "to_assignee_id", "assignee_id", None),
    ):
        row = connection.execute(
            select(table.c[field])
            .where(table.c.asset_id == asset["id"], table.c.result_version <= version)
            .order_by(table.c.result_version.desc())
            .limit(1)
        ).one_or_none()
        value = row[0] if row else None
        if row is None and baseline:
            value = connection.execute(
                select(db.asset_initial_facts.c[baseline]).where(
                    db.asset_initial_facts.c.asset_id == asset["id"]
                )
            ).scalar_one_or_none()
        facts[name] = value
    return jsonable_encoder(facts)


def _validate(operation):
    payload = operation.payload
    if operation.operation in VERSIONED:
        if operation.entity_type != "ASSET" or operation.expected_version is None:
            raise AssetInvalid("Asset identity and expected_version are required")
        # The envelope owns the occurrence claim and expected version. Payloads cannot
        # silently override either, even with equal values.
        if {"expected_version", "occurred_at", "client_op_id"} & payload.keys():
            raise AssetInvalid("Capture concurrency and time claims belong in the envelope")
        return (
            MODELS[operation.operation]
            .model_validate(
                dict(
                    payload,
                    expected_version=operation.expected_version,
                    occurred_at=operation.occurred_at,
                )
            )
            .model_dump()
        )
    if operation.operation == "RECEIVE_SCAN":
        if operation.entity_type != "RECEIPT":
            raise AssetInvalid("Receive scan requires an existing open Receipt")
        return ReceiveScan.model_validate(payload)
    if operation.operation == "ATTACH_EVIDENCE":
        request = EvidencePayload.model_validate(payload)
        return request, AttachmentLinkCreate.model_validate(
            dict(
                entity_type=operation.entity_type,
                entity_id=operation.entity_id,
                link_role=request.link_role,
            )
        ).model_dump()
    if operation.operation == "RESOLVE":
        if operation.entity_type != "EXCEPTION":
            raise AssetInvalid("Resolution requires an Exception")
        request = ResolvePayload.model_validate(payload)
        return dict(
            expected_status=request.expected_status,
            to_status="RESOLVED",
            note=request.note,
            occurred_at=operation.occurred_at,
        )
    raise AssetInvalid("Unsupported capture operation")


def _apply(connection, identity, operation, values, storage):
    if operation.operation in VERSIONED:
        values = dict(values, client_op_id=operation.operation_id)
        if operation.operation == "MOVE":
            return dict(asset_facts.move_asset(connection, operation.entity_id, values=values))
        if operation.operation == "ASSIGN":
            return dict(assignments.assign_asset(connection, operation.entity_id, values=values))
        if operation.operation == "UNASSIGN":
            return dict(assignments.unassign_asset(connection, operation.entity_id, values=values))
        return dict(
            transition_asset(connection, operation.entity_id, values=values, storage=storage)
        )
    if operation.operation == "RECEIVE_SCAN":
        # Existing receiving binds lines to this header's claim; preserve the queued
        # operation occurrence separately rather than rewriting the immutable header.
        result = receiving.add_line(
            connection, operation.entity_id, values=values.line.model_dump()
        )
        if values.reconcile:
            receiving.reconcile_receipt(connection, operation.entity_id)
        return {
            "receipt_id": result["id"],
            "line_id": result["lines"][-1]["id"] if result["lines"] else None,
        }
    if operation.operation == "ATTACH_EVIDENCE":
        request, link_values = values
        return dict(evidence.link(connection, storage, request.attachment_id, link_values))
    return dict(
        exception_workflow.transition_exception(connection, operation.entity_id, values=values)
    )


def process(connection, identity, operation, storage):
    """Lock idempotency, then epoch ordering, then the shared Asset row, in that order."""
    # A transaction advisory lock closes the absent-row race without provisional
    # writes. Its hash collisions only serialize unrelated UUIDs, never merge them.
    key = int.from_bytes(
        hashlib.sha256(operation.operation_id.bytes).digest()[:8], "big", signed=True
    )
    connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    table = db.capture_operations
    original = (
        connection.execute(select(table).where(table.c.operation_id == operation.operation_id))
        .mappings()
        .one_or_none()
    )
    if original is not None:
        if original["actor_id"] != identity.actor_id:
            raise AssetInvalid("Operation is unavailable to this credential")
        return _out(original, duplicate=True)
    flags = _sequence(connection, identity, operation)
    state = "APPLIED"
    try:
        with connection.begin_nested():
            if operation.actor_id != identity.actor_id:
                raise AssetInvalid("Queued Actor does not match the current credential")
            values = _validate(operation)
            if operation.operation in VERSIONED:
                asset = (
                    connection.execute(
                        select(db.assets)
                        .where(db.assets.c.id == operation.entity_id)
                        .with_for_update()
                    )
                    .mappings()
                    .one_or_none()
                )
                if asset is None:
                    raise AssetNotFound("Asset not found")
                if asset["version"] != operation.expected_version:
                    # Conflict observation and terminal record commit together. No
                    # domain operation is called, retried or rebased against a new version.
                    identifier = uuid7()
                    expected = _facts(connection, asset, operation.expected_version)
                    current = _facts(connection, asset, asset["version"])
                    connection.execute(
                        db.sync_conflicts.insert().values(
                            id=identifier,
                            org_id=identity.organization_id,
                            operation_id=operation.operation_id,
                            asset_id=asset["id"],
                            expected_version=operation.expected_version,
                            current_version=asset["version"],
                            expected_facts=expected,
                            current_facts=current,
                            occurred_at=operation.occurred_at,
                        )
                    )
                    result = dict(
                        code="SYNC_CONFLICT",
                        exception_id=identifier,
                        expected=expected,
                        current=current,
                    )
                    state = "REJECTED"
                else:
                    result = _apply(connection, identity, operation, values, storage)
            else:
                result = _apply(connection, identity, operation, values, storage)
    except (ValidationError, *FAILURES) as error:
        state = "REJECTED"
        # Bounded classifications, never SQL diagnostics, bearer data or caller values.
        code = (
            "NOT_FOUND"
            if isinstance(
                error, AssetNotFound | receiving.ReceivingNotFound | evidence.EvidenceNotFound
            )
            else "INVALID_OPERATION"
        )
        result = {"code": code}
    row = (
        connection.execute(
            table.insert()
            .values(
                operation_id=operation.operation_id,
                org_id=identity.organization_id,
                claimed_actor_id=operation.actor_id,
                client_id=operation.client_id,
                client_epoch=operation.client_epoch,
                client_seq=operation.client_seq,
                entity_type=operation.entity_type,
                entity_id=operation.entity_id,
                expected_version=operation.expected_version,
                operation=operation.operation,
                payload=operation.payload,
                occurred_at=operation.occurred_at,
                sync_state=state,
                result=jsonable_encoder(result),
                sequence_flags=flags,
            )
            .returning(table)
        )
        .mappings()
        .one()
    )
    return _out(row)


def batch(engine, raw_token, operations, storage):
    """Commit separately; an expired credential never authorizes the remaining queue."""
    responses = [None] * len(operations)
    for index, operation in ordered(operations):
        try:
            with engine.begin() as connection:
                identity = resolve_identity(connection, raw_token)
                if identity is None:
                    raise CredentialExpired()
                set_organization(connection, identity.organization_id)
                set_credential_context(connection, identity.token_digest)
                responses[index] = process(connection, identity, operation, storage)
        except (DBAPIError, AssetInvalid):
            # Global UUID collision from another tenant, lost credential context,
            # or invalid epoch binding rolls back this operation only.
            responses[index] = dict(
                operation_id=operation.operation_id,
                sync_state="REJECTED",
                recorded_at=None,
                result={"code": "OPERATION_UNAVAILABLE"},
                sequence_flags=[],
            )
    return responses
