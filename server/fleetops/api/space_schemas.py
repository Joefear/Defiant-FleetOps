"""Strict space inputs exclude tenant, identity, performer, and server timestamps."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, StringConstraints

from fleetops.api.schemas import InputModel
from fleetops.domain.location_kinds import LocationKind
from fleetops.domain.space import validate_timezone

SpaceText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
IanaTimezone = Annotated[SpaceText, AfterValidator(validate_timezone)]


class FacilityCreate(InputModel):
    """Facility facts only; timezone is local metadata rather than timestamp authority."""

    name: SpaceText
    timezone: IanaTimezone
    active: bool = True


class FacilityOut(BaseModel):
    """Persisted facility identity, context, and authenticated attribution."""

    id: UUID
    org_id: UUID
    name: str
    timezone: str
    active: bool
    created_by_actor_id: UUID
    created_at: datetime


class LocationCreate(InputModel):
    """Create a root or child; preserve code case while trimming surrounding whitespace."""

    facility_id: UUID
    parent_location_id: UUID | None = None
    code: SpaceText
    name: SpaceText
    kind: LocationKind
    active: bool = True


class LocationOut(BaseModel):
    """Persisted space facts, without movement, occupancy, or assignment."""

    id: UUID
    org_id: UUID
    facility_id: UUID
    parent_location_id: UUID | None
    code: str
    name: str
    kind: LocationKind
    active: bool
    created_by_actor_id: UUID
    created_at: datetime
