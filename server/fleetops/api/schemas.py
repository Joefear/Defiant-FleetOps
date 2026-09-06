"""Request data carries domain values, never tenant or performer authority."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from fleetops.domain.actor_types import ActorType
from fleetops.domain.party_roles import PartyRole

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Username = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^\S(?:.*\S)?$")]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(InputModel):
    username: Username
    password: SecretStr


class ActorCreate(InputModel):
    type: ActorType
    display_name: DisplayName


class PartyCreate(InputModel):
    display_name: DisplayName
    roles: set[PartyRole] = Field(min_length=1)


class ActorOut(BaseModel):
    id: UUID
    org_id: UUID
    type: ActorType
    display_name: str
    active: bool
    created_by_actor_id: UUID
    created_at: datetime


class PartyOut(BaseModel):
    id: UUID
    org_id: UUID
    display_name: str
    created_by_actor_id: UUID
    created_at: datetime
    roles: list[PartyRole]


class IdentityOut(BaseModel):
    org_id: UUID
    user_id: UUID
    actor_id: UUID


class SessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
