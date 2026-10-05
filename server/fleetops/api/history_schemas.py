"""Read-only history envelopes retain raw facts and explicit timestamp provenance."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, JsonValue

from fleetops.api.asset_schemas import AssetOut
from fleetops.api.record_correction_schemas import CorrectionIssueOut


class HistoryEntry(BaseModel):
    """Presentation order is occurrence time; result_version remains global authority."""

    kind: Literal[
        "TRANSITION",
        "MOVEMENT",
        "CUSTODY",
        "OWNERSHIP",
        "ASSIGNMENT",
        "INITIAL_FACTS",
        "INITIAL_ASSIGNMENT",
        "IDENTIFIER",
        "CONFIGURATION",
        "RECEIPT_LINE",
        "RECEIPT",
        "RECEIPT_COMPARATOR",
        "RECEIPT_CORRECTION",
        "PROCUREMENT_LINE",
        "PROCUREMENT_CORRECTION",
        "EXCEPTION",
        "EXCEPTION_EVENT",
        "SYNC_CONFLICT",
        "SYNC_CONFLICT_EVENT",
        "ATTACHMENT",
        "ATTACHMENT_LINK",
    ]
    id: UUID
    actor_id: UUID
    occurred_at: datetime
    recorded_at: datetime
    occurred_at_source: Literal["claimed", "recorded_link", "recorded_creation"]
    result_version: int | None
    cancels_history_id: UUID | None
    facts: dict[str, JsonValue]


class AssetHistoryOut(BaseModel):
    """Current projections and all retained evidence come from the same SQL snapshot."""

    asset: AssetOut
    entries: list[HistoryEntry]
    # Visible allocation order, independent of occurrence presentation; last is current.
    configuration_order: list[UUID]


class ReconciliationOut(BaseModel):
    """Counts unique affected records; detailed categories retain their original meaning."""

    discrepancy_count: int
    affected_asset_count: int
    affected_record_count: int
    assets: list[dict[str, JsonValue]]
    corrections: list[CorrectionIssueOut]
