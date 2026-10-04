"""Queued data is a claim; execution Actor, tenant and record time are server-owned."""

import json
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StrictBool, StrictInt, model_validator

from fleetops.api.asset_schemas import Description
from fleetops.api.receiving_schemas import ReceiptLineCreate
from fleetops.api.schemas import InputModel
from fleetops.capture.types import CaptureOperationKind

Sequence = Annotated[StrictInt, Field(gt=0, le=9223372036854775807)]
Version = Annotated[StrictInt, Field(gt=0, le=2147483647)]


class CaptureOperation(InputModel):
    """Immutable request identity with bounded data and no execution authority."""

    operation_id: UUID
    actor_id: UUID
    client_id: UUID
    client_epoch: UUID
    client_seq: Sequence
    entity_type: Annotated[str, Field(min_length=1, max_length=64)]
    entity_id: UUID
    expected_version: Version | None = None
    operation: CaptureOperationKind
    payload: dict[str, Any]
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def bounded_payload(self):
        # PostgreSQL's JSONB spacing can expand compact input; leave room for it.
        if len(json.dumps(self.payload, ensure_ascii=False, allow_nan=False).encode()) > 32000:
            raise ValueError("Capture payload is too large")
        return self


class CaptureBatch(InputModel):
    """Bound processing work while preserving each operation transaction boundary."""

    operations: list[CaptureOperation] = Field(min_length=1, max_length=100)


class ReceiveScan(InputModel):
    """Append to an existing open receipt; optional completion remains receipt-local."""

    line: ReceiptLineCreate
    reconcile: StrictBool = False


class ResolvePayload(InputModel):
    """Human resolution still compares the independent Exception status."""

    expected_status: Literal["OPEN", "ACKNOWLEDGED"]
    note: Description


class EvidencePayload(InputModel):
    """Link captured bytes; an operation cannot name arbitrary storage paths."""

    attachment_id: UUID
    link_role: str


class OperationOut(BaseModel):
    """Replay preserves the original result, record time and sequence flags."""

    operation_id: UUID
    sync_state: Literal["APPLIED", "REJECTED", "DUPLICATE"]
    recorded_at: datetime | None
    result: dict[str, Any]
    sequence_flags: list[str]


class ResolvedEntity(BaseModel):
    """Expose explicit domain summaries without credential or storage internals."""

    entity_type: str
    entity_id: UUID
    summary: dict[str, Any]


class SyncConflictOut(BaseModel):
    """OPEN is the immutable origin; subsequent status comes only from ordered events."""

    id: UUID
    org_id: UUID
    exception_type: Literal["SYNC_CONFLICT"]
    severity: Literal["UNSPECIFIED"]
    entity_type: Literal["ASSET"]
    entity_id: UUID
    asset_id: UUID
    operation_id: UUID
    expected_version: int
    current_version: int
    expected_facts: dict[str, Any]
    current_facts: dict[str, Any]
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime
    opened_by_actor_id: UUID
    opened_at: datetime
    status: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"]
    event_seq: int
    resolved_by_actor_id: UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
    events: list[dict[str, Any]]
