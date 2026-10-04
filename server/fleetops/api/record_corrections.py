"""Explicit procurement, receipt and Exception verbs reuse authenticated transactions."""

from collections.abc import Callable, Iterator
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import select, text

from fleetops.api.assets import asset_errors
from fleetops.api.capture_schemas import SyncConflictOut
from fleetops.api.context import RequestContext
from fleetops.api.procurement import procurement_errors
from fleetops.api.receiving import receiving_errors
from fleetops.api.record_correction_schemas import (
    CorrectionIssueOut,
    EffectiveProcurementLineOut,
    EffectiveReceiptLineOut,
    ExceptionEventOut,
    ExceptionOut,
    ExceptionTransition,
    ProcurementCorrection,
    ProcurementCorrectionOut,
    ReceiptCorrectionOut,
    ReceiptLineCorrection,
)
from fleetops.db.metadata import (
    purchase_order_line_corrections,
    receipt_line_corrections,
    receipt_lines,
)
from fleetops.domain import exception_workflow, procurement, receiving, record_corrections


def create_record_correction_router(authenticated: Callable[..., Iterator[RequestContext]]):
    """All consequence writes share the same rollback and credential boundary."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post(
        "/purchase-orders/{po_id}/lines/{root_id}/corrections",
        response_model=ProcurementCorrectionOut,
        status_code=201,
    )
    def correct_procurement(
        po_id: UUID, root_id: UUID, body: ProcurementCorrection, context: Context
    ):
        """Correct an unreferenced issued line without changing supersession lineage."""
        with asset_errors(), procurement_errors():
            return record_corrections.correct_procurement(
                context.connection, po_id, root_id, values=body.model_dump(exclude_unset=True)
            )

    @router.get(
        "/purchase-orders/{po_id}/lines/{root_id}/corrections",
        response_model=list[ProcurementCorrectionOut],
    )
    def procurement_history(po_id: UUID, root_id: UUID, context: Context):
        """Read all generations under the typed visible parent."""
        with asset_errors(), procurement_errors():
            procurement.get_line(context.connection, po_id, root_id)
            return record_corrections.record_history(
                context.connection, purchase_order_line_corrections, "po_line_id", root_id
            )

    @router.post(
        "/receipts/{receipt_id}/lines/{root_id}/corrections",
        response_model=ReceiptCorrectionOut,
        status_code=201,
    )
    def correct_receipt(
        receipt_id: UUID, root_id: UUID, body: ReceiptLineCorrection, context: Context
    ):
        """Commit a pair, pinned evaluation and required Exception consequences atomically."""
        with asset_errors(), receiving_errors():
            return record_corrections.correct_receipt_line(
                context.connection, receipt_id, root_id, values=body.model_dump(exclude_unset=True)
            )

    @router.get(
        "/receipts/{receipt_id}/lines/{root_id}/corrections",
        response_model=list[ReceiptCorrectionOut],
    )
    def receipt_history(receipt_id: UUID, root_id: UUID, context: Context):
        """Raw typed audit never substitutes correction UUIDs for original receipt-line identity."""
        with asset_errors(), receiving_errors():
            header = receiving.get_receipt(context.connection, receipt_id)
            if not any(line["id"] == root_id for line in header["lines"]):
                raise receiving.ReceivingNotFound("Receipt line not found")
            return record_corrections.record_history(
                context.connection, receipt_line_corrections, "receipt_line_id", root_id
            )

    @router.post(
        "/exceptions/{exception_id}/events", response_model=ExceptionEventOut, status_code=201
    )
    def transition(exception_id: UUID, body: ExceptionTransition, context: Context):
        """Admit one legal optimistic status transition from event-derived authority."""
        with asset_errors():
            return exception_workflow.transition_exception(
                context.connection, exception_id, values=body.model_dump()
            )

    @router.get("/exceptions/open", response_model=list[ExceptionOut | SyncConflictOut])
    def open_exceptions(
        context: Context,
        receipt_id: UUID | None = None,
        receipt_line_id: UUID | None = None,
        po_line_id: UUID | None = None,
        asset_id: UUID | None = None,
        entity_type: Literal["RECEIPT", "RECEIPT_LINE", "ASSET"] | None = None,
        entity_id: UUID | None = None,
    ):
        """Find unresolved observations by explicit tenant-safe typed relationships."""
        with asset_errors():
            return exception_workflow.list_open(
                context.connection,
                receipt_id=receipt_id,
                receipt_line_id=receipt_line_id,
                po_line_id=po_line_id,
                asset_id=asset_id,
                entity_type=entity_type,
                entity_id=entity_id,
            )

    @router.get("/exceptions/{exception_id}", response_model=ExceptionOut | SyncConflictOut)
    def exception(exception_id: UUID, context: Context):
        """Expose immutable observation alongside status projection and event history."""
        with asset_errors():
            return exception_workflow.get_exception(context.connection, exception_id)

    @router.get("/health/corrections", response_model=list[CorrectionIssueOut])
    def health(context: Context):
        """Reconstruct correction, evaluation and workflow authority without repair."""
        # Load correction-aware health after router/domain initialization;
        # corrections and the shared asset history reader depend on each other.
        from fleetops.domain.corrections import lifecycle_correction_issues

        rows = list(
            context.connection.execute(
                text(
                    "SELECT DISTINCT * FROM fleetops.correction_anomalies ORDER BY "
                    "entity_type,entity_id,issue"
                )
            ).mappings()
        )
        return rows + lifecycle_correction_issues(context.connection)

    @router.get(
        "/purchase-orders/{po_id}/lines/{root_id}/effective",
        response_model=EffectiveProcurementLineOut,
    )
    def effective_procurement(po_id: UUID, root_id: UUID, context: Context):
        """Return the ordinary root alongside its deterministic effective replacement."""
        with asset_errors(), procurement_errors():
            root = procurement.get_line(context.connection, po_id, root_id)
            head = record_corrections._head(
                context.connection, purchase_order_line_corrections, "po_line_id", root_id, root
            )
            generation = head["correction_generation"]
            return dict(
                root=root,
                effective=head,
                correction_generation=generation,
                source_id=head["id"] if generation else None,
            )

    @router.get(
        "/receipts/{receipt_id}/lines/{root_id}/effective", response_model=EffectiveReceiptLineOut
    )
    def effective_receipt(receipt_id: UUID, root_id: UUID, context: Context):
        """Preserve captured identity while exposing the newest typed replacement facts."""
        with asset_errors(), receiving_errors():
            root = (
                context.connection.execute(
                    select(receipt_lines).where(
                        receipt_lines.c.id == root_id, receipt_lines.c.receipt_id == receipt_id
                    )
                )
                .mappings()
                .one_or_none()
            )
            if root is None:
                raise receiving.ReceivingNotFound("Receipt line not found")
            head = record_corrections._head(
                context.connection, receipt_line_corrections, "receipt_line_id", root_id, root
            )
            generation = head["correction_generation"]
            return dict(
                root=root,
                effective=head,
                correction_generation=generation,
                source_id=head["id"] if generation else None,
            )

    return router
