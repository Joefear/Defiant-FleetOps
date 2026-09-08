"""Strict Asset inputs exclude identity, tenant, performer and projection authority."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, StrictInt, StringConstraints, model_validator

from fleetops.api.schemas import InputModel
from fleetops.domain.identifier_types import IdentifierType
from fleetops.domain.lifecycle import AssetState

Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Description = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


class AssetPatch(InputModel):
    """Only descriptive/display fields; explicit NULL and empty PATCH are invalid."""

    asset_tag: Tag | None = None
    description: Description | None = None

    @model_validator(mode="after")
    def require_edit(self):
        """Omission preserves existing text; NULL must not erase required facts."""
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Supply non-null asset_tag or description")
        return self


class TransitionRequest(InputModel):
    """Event time is a claim; expected version expresses concurrency, never authority."""

    expected_version: Annotated[StrictInt, Field(gt=0, le=2147483647)]
    from_state: AssetState
    to_state: AssetState
    reason: Description
    occurred_at: AwareDatetime


class AssetOut(BaseModel):
    """Current Asset facts and descriptive attribution, addressed by permanent UUID."""

    id: UUID
    org_id: UUID
    item_id: UUID
    asset_tag: str
    description: str
    owner_party_id: UUID
    custodian_party_id: UUID | None
    current_location_id: UUID | None
    current_assignment_id: UUID | None
    current_state: AssetState
    version: int
    created_by_actor_id: UUID
    updated_by_actor_id: UUID
    created_at: datetime
    updated_at: datetime


class IdentifierOut(BaseModel):
    """An unreadable marker has no readable value and carries its recorded reason."""

    id: UUID
    org_id: UUID
    asset_id: UUID
    type: IdentifierType
    value: str | None
    unreadable_reason: str | None
    created_by_actor_id: UUID
    created_at: datetime


class TransitionOut(BaseModel):
    """Immutable state history; result_version is a global produced Asset version."""

    id: UUID
    org_id: UUID
    asset_id: UUID
    result_version: int
    from_state: AssetState | None
    to_state: AssetState
    reason: str
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime
    evidence_ref: UUID | None
    corrects_transition_id: UUID | None
    client_op_id: UUID | None


class StateDiscrepancy(BaseModel):
    """Tenant-visible disagreement with authoritative state history, without repair."""

    asset_id: UUID
    current_state: AssetState
    version: int
    latest_state: AssetState | None
    latest_result_version: int | None
    discrepancies: list[str]
