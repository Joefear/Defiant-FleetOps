"""Bounded Slice 7 business inputs; credential and ordering authority stay in the database."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, computed_field

from fleetops.api.asset_schemas import PhysicalChangeOut, PhysicalChangeRequest, Tag
from fleetops.api.schemas import InputModel

AssigneeType = Literal["ACTOR", "LOCATION", "PARTY"]


class AssignmentRequest(PhysicalChangeRequest):
    """One target pair; history decides ASSIGN versus REASSIGN."""

    assignee_type: AssigneeType
    assignee_id: UUID


class AssignmentEventOut(PhysicalChangeOut):
    """Event class is derived from pairs, never another mutable history field."""

    from_assignee_type: AssigneeType | None
    from_assignee_id: UUID | None
    to_assignee_type: AssigneeType | None
    to_assignee_id: UUID | None
    corrects_assignment_event_id: UUID | None

    @computed_field
    @property
    def event_type(self) -> Literal["ASSIGN", "UNASSIGN", "REASSIGN"]:
        """Classify the immutable pair semantics for consumers of the history API."""
        if self.to_assignee_id is None:
            return "UNASSIGN"
        return "ASSIGN" if self.from_assignee_id is None else "REASSIGN"


class InitialAssignmentOut(BaseModel):
    """Presence is a positive initially-unassigned witness, not an unknown assignee."""

    asset_id: UUID
    org_id: UUID
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime


class AssignmentIntervalOut(BaseModel):
    """Times are original claims; successor selection follows event versions."""

    establishing_event_id: UUID
    assignee_type: AssigneeType
    assignee_id: UUID
    started_at: datetime
    ended_at: datetime | None


class AssignmentHistoryOut(BaseModel):
    """Expose missing creation truth honestly while preserving every event."""

    initial_assignment_fact: InitialAssignmentOut | None
    events: list[AssignmentEventOut]
    intervals: list[AssignmentIntervalOut]


class ConfigurationRequest(InputModel):
    """Application time and configuration values only; no Actor, sequence or evidence selector."""

    image_name: Tag
    image_version: Tag
    config_profile: Tag
    notes: str = Field(default="", max_length=4000)
    applied_at: AwareDatetime


class ConfigurationOut(BaseModel):
    """History is ordered internally; global allocation counts are not an API field."""

    id: UUID
    org_id: UUID
    asset_id: UUID
    image_name: str
    image_version: str
    config_profile: str
    notes: str
    applied_by: UUID
    applied_at: datetime
    recorded_at: datetime
    evidence_ref: UUID | None
