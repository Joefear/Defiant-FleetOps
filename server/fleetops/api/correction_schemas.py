"""Bounded correction inputs never accept pair, role, actor or produced-version authority."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, StrictInt, model_validator

from fleetops.api.asset_schemas import Description
from fleetops.api.schemas import InputModel
from fleetops.domain.lifecycle import AssetState


class AssetCorrection(InputModel):
    """Administrative time is distinct from optional replacement domain time."""

    expected_version: Annotated[StrictInt, Field(gt=0, le=2147483647)]
    reason: Description
    correction_occurred_at: AwareDatetime
    occurred_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def require_domain_time_if_supplied(self):
        """Omission preserves effective domain time; explicit null cannot erase it."""
        if "occurred_at" in self.model_fields_set and self.occurred_at is None:
            raise ValueError("Domain occurrence time cannot be null")
        return self


class TransitionCorrection(AssetCorrection):
    """Pre-root source is derived; the caller supplies only the replacement destination."""

    to_state: AssetState


class MovementCorrection(AssetCorrection):
    """Explicit unknown location remains a valid recorded fact."""

    to_location_id: UUID | None


class CustodyCorrection(AssetCorrection):
    """A missing represented custodian is independent of ownership."""

    to_custodian_party_id: UUID | None


class OwnershipCorrection(AssetCorrection):
    """Ownership always names a represented Party."""

    to_owner_party_id: UUID


class AssignmentCorrection(AssetCorrection):
    """Both endpoint fields are supplied together, including explicit unassignment."""

    to_assignee_type: Literal["ACTOR", "LOCATION", "PARTY"] | None
    to_assignee_id: UUID | None

    @model_validator(mode="after")
    def paired_target(self):
        """A partial polymorphic endpoint has no defined assignment meaning."""
        if (self.to_assignee_type is None) != (self.to_assignee_id is None):
            raise ValueError("Supply both target fields or set both to null")
        return self
