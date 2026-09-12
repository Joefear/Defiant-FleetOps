"""Authenticated receiving verbs; clients supply facts and exact comparator targets."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from fleetops.api.context import RequestContext
from fleetops.api.receiving_schemas import ReceiptCreate, ReceiptLineCreate, ReceiptOut
from fleetops.api.schemas import InputModel
from fleetops.domain import receiving

EMPTY_RECONCILE = InputModel()


@contextmanager
def receiving_errors():
    """Expose bounded errors without database values or tenant-existence details."""
    try:
        yield
    except receiving.ReceivingNotFound as error:
        raise HTTPException(404, str(error)) from error
    except receiving.ReceivingConflict as error:
        raise HTTPException(409, str(error)) from error
    except receiving.ReceivingInvalid as error:
        raise HTTPException(422, str(error)) from error


def create_receiving_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse the shared credential-derived transaction for all receiving consequences."""
    router = APIRouter(prefix="/receipts")
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post("", response_model=ReceiptOut, status_code=201)
    def create(body: ReceiptCreate, context: Context):
        """Capture a complete delivery or open a receipt for individual unit observations."""
        with receiving_errors():
            return receiving.create_receipt(
                context.connection,
                org_id=context.identity.organization_id,
                values=body.model_dump(),
            )

    @router.get("", response_model=list[ReceiptOut])
    def list_records(context: Context):
        """Read tenant-visible immutable receipts."""
        return receiving.list_receipts(context.connection)

    @router.get("/{receipt_id}", response_model=ReceiptOut)
    def get(receipt_id: UUID, context: Context):
        """Address receiving history by its permanent opaque identity."""
        with receiving_errors():
            return receiving.get_receipt(context.connection, receipt_id)

    @router.post("/{receipt_id}/lines", response_model=ReceiptOut, status_code=201)
    def append(receipt_id: UUID, body: ReceiptLineCreate, context: Context):
        """Append one physical observation while the receipt population remains open."""
        with receiving_errors():
            return receiving.add_line(context.connection, receipt_id, values=body.model_dump())

    @router.post("/{receipt_id}/reconcile", response_model=ReceiptOut)
    def reconcile(
        receipt_id: UUID, context: Context, body: Annotated[InputModel, Body()] = EMPTY_RECONCILE
    ):
        """Record immutable quantity conclusions after all physical lines have been captured."""
        with receiving_errors():
            return receiving.reconcile_receipt(context.connection, receipt_id)

    return router
