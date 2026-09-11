"""Procurement requests contain business inputs; UUIDs, UOM, time and Actor are server-owned."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, model_validator

from fleetops.api.schemas import InputModel
from fleetops.domain.procurement_status import PurchaseOrderStatus
from fleetops.domain.uom import UnitOfMeasure

Number = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Notes = Annotated[str, StringConstraints(max_length=4000)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Price = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class OrderCreate(InputModel):
    """A vendor is a same-tenant VENDOR Party; status begins DRAFT."""

    vendor_party_id: UUID
    po_number: Number
    notes: Notes | None = None


class DraftUpdate(InputModel):
    """Omission preserves fields; only explicitly nullable business fields can be cleared."""

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> Self:
        for name in self.model_fields_set - {"notes", "expected_date"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class OrderUpdate(DraftUpdate):
    """Status and issuance have their own operation, never a generic PATCH selector."""

    vendor_party_id: UUID | None = None
    po_number: Number | None = None
    notes: Notes | None = None


class AmendmentCreate(InputModel):
    """The route selects an exact predecessor; the service retains its logical line number."""

    item_id: UUID
    quantity: Quantity
    unit_price: Price
    expected_date: date | None = None


class LineCreate(AmendmentCreate):
    """Root authoring chooses a positive business line number, not record identity."""

    line_number: Annotated[int, Field(strict=True, gt=0, le=2147483647)]


class LineUpdate(DraftUpdate):
    """UOM and lineage remain immutable even during draft authoring."""

    item_id: UUID | None = None
    quantity: Quantity | None = None
    unit_price: Price | None = None
    expected_date: date | None = None


class AttributionOut(BaseModel):
    """Database-recorded creator/updater facts, frozen with issued history."""

    id: UUID
    org_id: UUID
    created_by_actor_id: UUID
    updated_by_actor_id: UUID
    created_at: datetime
    updated_at: datetime


class OrderOut(AttributionOut):
    """Issuance records its own performer separately from original creation."""

    vendor_party_id: UUID
    po_number: str
    notes: str | None
    status: PurchaseOrderStatus
    issued_at: datetime | None
    issued_by_actor_id: UUID | None


class LineOut(AttributionOut):
    """Every version exposes its backward reference and derived leaf status."""

    po_id: UUID
    line_number: int
    item_id: UUID
    quantity: Decimal
    uom: UnitOfMeasure
    unit_price: Decimal
    expected_date: date | None
    supersedes_line_id: UUID | None
    active: bool
    superseded: bool
