"""Strict label requests expose no tenant, Actor, output path or barcode expression."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, StringConstraints, field_validator

from fleetops.api.schemas import InputModel
from fleetops.labels.types import HumanField, OutputFormat, PrintStatus, Symbology


class TemplateCreate(InputModel):
    """D14 fixes the barcode field even when other human-readable fields are chosen."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    entity_type: Literal["ASSET"] = "ASSET"
    human_fields: Annotated[list[HumanField], Field(min_length=1, max_length=4)]
    symbology: Symbology
    barcode_field: Literal["id"] = "id"

    @field_validator("name")
    @classmethod
    def printable_name(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Template name contains control characters")
        return value

    @field_validator("human_fields")
    @classmethod
    def unique_fields(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Human-readable fields must be distinct")
        return value


class TemplateOut(TemplateCreate):
    """Templates are immutable so queued requests retain their original meaning."""

    id: UUID
    org_id: UUID
    created_by_actor_id: UUID
    created_at: datetime


class PrintRequest(InputModel):
    """Queue a durable job; the server selects the corresponding registered adapter."""

    template_id: UUID
    output_format: OutputFormat = OutputFormat.PNG


class PrintJobOut(BaseModel):
    """Artifact paths and rendering internals are intentionally not public."""

    id: UUID
    org_id: UUID
    template_id: UUID
    entity_type: Literal["ASSET"]
    entity_id: UUID
    requested_by: UUID
    requested_at: datetime
    status: PrintStatus
    adapter_name: str
    output_format: OutputFormat
    attempts: int
    artifact_sha256: str | None
    error_code: str | None
    updated_by_actor_id: UUID | None
    updated_at: datetime | None
