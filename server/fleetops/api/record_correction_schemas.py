"""Typed bounded record corrections and Exception workflow inputs."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, model_validator

from fleetops.api.asset_schemas import Description
from fleetops.api.procurement_schemas import LineOut
from fleetops.api.receiving_schemas import (
    Notes,
    PackingQuantity,
    Quantity,
    ReceiptLineOut,
    ReceivingExceptionOut,
)
from fleetops.api.schemas import InputModel
from fleetops.domain.exception_types import ExceptionEntityType, ExceptionSeverity, ExceptionType
from fleetops.domain.identifier_types import IdentifierType
from fleetops.domain.receiving_types import ReceiptCondition
from fleetops.domain.uom import UnitOfMeasure

Generation = Annotated[StrictInt, Field(ge=0, le=2147483647)]
Status = Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "WAIVED"]


class RecordCorrection(InputModel):
    """An optimistic generation token is checked only after the owning lock."""

    expected_generation: Generation
    reason: Description
    correction_occurred_at: AwareDatetime


class ProcurementCorrection(RecordCorrection):
    """Partial business values are materialized from the locked effective generation."""

    item_id: UUID | None = None
    quantity: Quantity | None = None
    uom: UnitOfMeasure | None = None
    unit_price: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)] | None = None
    expected_date: date | None = None

    @model_validator(mode="after")
    def required_business_values(self):
        """Omission retains a required value; explicit null cannot erase it."""
        for field in ("item_id", "quantity", "uom", "unit_price"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ReceiptLineCorrection(RecordCorrection):
    """Creation identity and witnesses are intentionally absent from correctable input."""

    po_line_id: UUID | None = None
    expected_po_generation: Generation = 0
    item_id: UUID | None = None
    quantity: Quantity | None = None
    uom: UnitOfMeasure | None = None
    condition: ReceiptCondition | None = None
    packing_quantity: PackingQuantity | None = None
    notes: Notes | None = None
    observed_identifier_type: IdentifierType | None = None
    observed_identifier_value: Annotated[str, Field(min_length=1, max_length=500)] | None = None

    @model_validator(mode="after")
    def observation_shapes(self):
        """Materialization may retain a whole endpoint, but cannot accept half a patch."""
        for field in ("item_id", "quantity", "uom", "condition"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        pair = {"observed_identifier_type", "observed_identifier_value"}
        if self.model_fields_set & pair and not pair <= self.model_fields_set:
            raise ValueError("Supply both observed identifier fields")
        return self


class ExceptionTransition(InputModel):
    """Terminal notes are operator statements; no caller selects attribution or sequence."""

    expected_status: Status
    to_status: Literal["ACKNOWLEDGED", "RESOLVED", "WAIVED"]
    note: Description | None = None
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def terminal_note(self):
        """A terminal disposition needs a nonblank human explanation."""
        if self.to_status in ("RESOLVED", "WAIVED") and self.note is None:
            raise ValueError("Terminal disposition requires a note")
        return self


class RecordCorrectionOut(BaseModel):
    """Expose pair authority and audit time without internal transaction identifiers."""

    id: UUID
    org_id: UUID
    cancels_history_id: UUID | None = None
    correction_role: Literal["REVERSAL", "CORRECTED"]
    correction_pair_id: UUID
    correction_generation: int
    correction_occurred_at: datetime
    reason: str
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime


class ProcurementCorrectionOut(RecordCorrectionOut):
    """Ordinary root identity remains distinct from its typed correction member."""

    po_id: UUID
    po_line_id: UUID
    item_id: UUID
    quantity: Decimal
    uom: UnitOfMeasure
    unit_price: Decimal | None
    expected_date: date | None


class ReceiptCorrectionOut(RecordCorrectionOut):
    """The replacement records line facts and preserves created Asset identity."""

    receipt_id: UUID
    receipt_line_id: UUID
    po_line_id: UUID | None
    item_id: UUID
    quantity: Decimal
    uom: UnitOfMeasure
    condition: ReceiptCondition
    packing_quantity: Decimal | None
    notes: str | None
    asset_id: UUID | None
    observed_identifier_type: IdentifierType | None
    observed_identifier_value: str | None
    conflicting_asset_id: UUID | None
    serialized: bool
    owner_party_id: UUID | None
    custodian_party_id: UUID | None


class ExceptionEventOut(BaseModel):
    """Event succession is sequence order even when occurrence clocks disagree."""

    id: UUID
    org_id: UUID
    exception_id: UUID
    event_seq: int
    from_status: Status
    to_status: Status
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime
    note: str | None
    evaluation_id: UUID | None


class ExceptionOut(ReceivingExceptionOut):
    """One immutable observation with its separately maintained workflow projection."""

    exception_type: ExceptionType
    severity: ExceptionSeverity
    entity_type: ExceptionEntityType
    entity_id: UUID
    opened_by_actor_id: UUID
    opened_at: datetime
    status: Status
    event_seq: int
    resolved_by_actor_id: UUID | None
    resolved_at: datetime | None
    resolution_note: str | None
    events: list[ExceptionEventOut]


class EffectiveReceiptLineOut(BaseModel):
    """Keep root capture and effective replacement explicit instead of rewriting either."""

    root: ReceiptLineOut
    effective: ReceiptCorrectionOut | ReceiptLineOut
    correction_generation: int
    source_id: UUID | None


class EffectiveProcurementLineOut(BaseModel):
    """Supersession identity and correction generation remain separate relationships."""

    root: LineOut
    effective: ProcurementCorrectionOut | LineOut
    correction_generation: int
    source_id: UUID | None


class CorrectionIssueOut(BaseModel):
    """A discrepancy identifies typed authority and never performs a repair."""

    org_id: UUID
    entity_type: Literal["ASSET", "PO_LINE", "RECEIPT_LINE", "EXCEPTION", "RECEIPT"]
    entity_id: UUID
    issue: str
