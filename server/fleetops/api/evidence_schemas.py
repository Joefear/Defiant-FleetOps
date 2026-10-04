"""Strict evidence requests exclude tenant, capturer, server time and storage-key authority."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, StringConstraints, field_validator

from fleetops.api.schemas import InputModel
from fleetops.evidence.types import EvidenceEntity, EvidenceRole, EvidenceSource


class UploadMetadata(InputModel):
    """Query metadata accompanies raw bytes; capture time remains the device's claim."""

    source_type: EvidenceSource
    captured_at: AwareDatetime
    original_filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    media_type: Annotated[
        str,
        StringConstraints(max_length=127, pattern=r"^[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+$"),
    ]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None
    supersedes_attachment_id: UUID | None = None

    @field_validator("original_filename")
    @classmethod
    def printable_filename(cls, value):
        """Keep control characters out of capture metadata and download names."""
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("Filename contains control characters")
        return value


class AttachmentOut(BaseModel):
    """Content identity and capture provenance; filesystem paths are never exposed."""

    id: UUID
    org_id: UUID
    sha256: str
    byte_size: int
    media_type: str
    source_type: EvidenceSource
    captured_by: UUID
    captured_at: datetime
    recorded_at: datetime
    supersedes_attachment_id: UUID | None
    original_filename: str


class AttachmentLinkCreate(InputModel):
    """Only bounded target identity and purpose are supplied by the caller."""

    entity_type: EvidenceEntity
    entity_id: UUID
    link_role: EvidenceRole


class AttachmentLinkOut(AttachmentLinkCreate):
    """Immutable link attribution is derived from the authenticated request."""

    id: UUID
    org_id: UUID
    attachment_id: UUID
    actor_id: UUID
    recorded_at: datetime
