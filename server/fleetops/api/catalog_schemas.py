"""Catalog input excludes server-owned identity, organization, timestamps, and attribution."""

from datetime import datetime
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, StringConstraints, model_validator

from fleetops.api.schemas import InputModel
from fleetops.domain.reference_types import ReferenceEntityType
from fleetops.domain.uom import UnitOfMeasure

CatalogText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
# External strings are matched exactly, including case and surrounding whitespace.
# Reject whitespace-only input without silently changing an external system's value.
ExternalSystem = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"\S")]
ExternalKind = Annotated[str, StringConstraints(min_length=1, max_length=100, pattern=r"\S")]
ExternalValue = Annotated[str, StringConstraints(min_length=1, max_length=500, pattern=r"\S")]


class ItemCreate(InputModel):
    """Catalog facts only; controlled items may await their precise classification."""

    manufacturer_party_id: UUID
    manufacturer_part_number: CatalogText
    revision: CatalogText | None = None
    description: Description
    uom: UnitOfMeasure
    serialized: bool
    export_classification: CatalogText | None = None
    export_controlled: bool = False
    active: bool = True


class ItemUpdate(InputModel):
    """Omission preserves a field; explicit NULL clears only revision or classification."""

    manufacturer_party_id: UUID | None = None
    manufacturer_part_number: CatalogText | None = None
    revision: CatalogText | None = None
    description: Description | None = None
    uom: UnitOfMeasure | None = None
    serialized: bool | None = None
    export_classification: CatalogText | None = None
    export_controlled: bool | None = None
    active: bool | None = None

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> Self:
        """PATCH must not turn nullable defaults in this model into nullable database facts."""
        for name in self.model_fields_set - {"revision", "export_classification"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ItemOut(BaseModel):
    """Opaque identity and current catalog facts, with server-recorded attribution."""

    id: UUID
    org_id: UUID
    manufacturer_party_id: UUID
    manufacturer_part_number: str
    revision: str | None
    description: str
    uom: UnitOfMeasure
    serialized: bool
    export_classification: str | None
    export_controlled: bool
    active: bool
    created_by_actor_id: UUID
    updated_by_actor_id: UUID
    created_at: datetime
    updated_at: datetime


class ReferenceCreate(InputModel):
    """Attachment always names its supported target by FleetOps opaque ID."""

    entity_type: ReferenceEntityType
    entity_id: UUID
    system: ExternalSystem
    reference_type: ExternalKind
    external_value: ExternalValue


class ReferenceOut(BaseModel):
    """Search results carry both attachment identity and the FleetOps target identity."""

    id: UUID
    org_id: UUID
    entity_type: ReferenceEntityType
    entity_id: UUID
    system: str
    reference_type: str
    external_value: str
    created_by_actor_id: UUID
    created_at: datetime
