"""Receiving inputs contain observations, never classification or identity authority."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from fleetops.api.asset_schemas import Description, Tag
from fleetops.api.schemas import InputModel
from fleetops.domain.identifier_types import IdentifierType
from fleetops.domain.receiving_types import ReceiptCondition, ReceivingExceptionType
from fleetops.domain.uom import UnitOfMeasure

Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
PackingQuantity = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Notes = Annotated[str, StringConstraints(max_length=4000)]
Reference = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class ReceivedIdentifier(InputModel):
    """Preserve readable values exactly; unreadability requires an explicit reason."""

    type: IdentifierType
    value: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    unreadable_reason: Notes | None = None

    @model_validator(mode="after")
    def one_observation(self) -> Self:
        if self.value is not None:
            if not self.value.strip() or self.unreadable_reason is not None:
                raise ValueError("Readable identifier requires a value and no unreadable reason")
        elif self.unreadable_reason is None or not self.unreadable_reason.strip():
            raise ValueError("Unreadable identifier requires a nonblank reason")
        return self


class ReceivedUnit(InputModel):
    """Explicit business facts for one physical serialized unit; Actor is never an input."""

    owner_party_id: UUID
    custodian_party_id: UUID | None = None
    asset_tag: Tag
    description: Description
    identifier: ReceivedIdentifier


class ComparatorBinding(InputModel):
    """Pin a specifically acknowledged effective generation of an ordinary PO line."""

    po_line_id: UUID
    expected_generation: Annotated[StrictInt, Field(ge=0, le=2147483647)]


class ReceiptLineCreate(InputModel):
    """One serialized unit or one nonserialized quantity observation, never an Asset selector."""

    po_line_id: UUID | None = None
    expected_po_generation: Annotated[StrictInt, Field(ge=0, le=2147483647)] = 0
    item_id: UUID
    quantity: Quantity
    uom: UnitOfMeasure
    condition: ReceiptCondition
    packing_quantity: PackingQuantity | None = None
    notes: Notes | None = None
    unit: ReceivedUnit | None = None


class ReceiptCreate(InputModel):
    """Capture one delivery or open a receipt for line-by-line capture before reconciliation."""

    vendor_party_id: UUID
    po_id: UUID | None = None
    dock_location_id: UUID | None = None
    packing_reference: Reference | None = None
    received_at: AwareDatetime
    comparator_ids: list[UUID] = Field(default_factory=list)
    comparator_bindings: list[ComparatorBinding] = Field(default_factory=list)
    lines: list[ReceiptLineCreate] = Field(default_factory=list)
    reconcile: StrictBool = True

    @model_validator(mode="after")
    def distinct_comparators(self) -> Self:
        targets = self.comparator_ids + [binding.po_line_id for binding in self.comparator_bindings]
        if len(targets) != len(set(targets)):
            raise ValueError("Comparator UUIDs must be distinct")
        if self.po_id is None and (
            targets or any(line.po_line_id is not None for line in self.lines)
        ):
            raise ValueError("A comparator requires a receipt PO")
        if self.packing_reference is not None and not self.packing_reference.strip():
            raise ValueError("Packing reference cannot be blank")
        return self


class ReceivingAttributionOut(BaseModel):
    """Opaque identity and separately recorded event-time and credential attribution."""

    id: UUID
    org_id: UUID
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime


class ReceiptLineOut(ReceivingAttributionOut):
    """Actual captured facts; observed conflict serials are separate from canonical identity."""

    receipt_id: UUID
    po_line_id: UUID | None
    item_id: UUID
    quantity: Decimal
    uom: UnitOfMeasure
    condition: ReceiptCondition
    packing_quantity: Decimal | None
    notes: str | None
    serialized: bool
    owner_party_id: UUID | None
    custodian_party_id: UUID | None
    asset_id: UUID | None
    observed_identifier_type: IdentifierType | None
    observed_identifier_value: str | None


class ReceivingExceptionOut(ReceivingAttributionOut):
    """Typed immutable relationships; conflicting Asset never means received identity."""

    evaluation_id: UUID | None
    exception_type: ReceivingExceptionType
    receipt_id: UUID
    receipt_line_id: UUID | None
    po_line_id: UUID | None
    asset_id: UUID | None
    conflicting_asset_id: UUID | None


class ReceiptComparatorOut(BaseModel):
    """A reopened capture client reads the immutable acknowledged expectation source."""

    po_line_id: UUID
    source_generation: int
    source_id: UUID | None


class ReceiptOut(ReceivingAttributionOut):
    """Receipt completion derives from its immutable witness, not a mutable PO projection."""

    vendor_party_id: UUID
    po_id: UUID | None
    dock_location_id: UUID | None
    packing_reference: str | None
    received_at: datetime
    reconciled: bool
    comparator_ids: list[UUID]
    comparator_bindings: list[ReceiptComparatorOut]
    lines: list[ReceiptLineOut]
    exceptions: list[ReceivingExceptionOut]
