"""Bounded authenticated procurement verbs; physical receiving remains a later slice."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException

from fleetops.api.context import RequestContext
from fleetops.api.procurement_schemas import (
    AmendmentCreate,
    LineCreate,
    LineOut,
    LineUpdate,
    OrderCreate,
    OrderOut,
    OrderUpdate,
)
from fleetops.api.schemas import InputModel
from fleetops.domain import procurement

EMPTY_ISSUE = InputModel()


@contextmanager
def procurement_errors():
    """Keep tenant existence and database constraint details out of error responses."""
    try:
        yield
    except procurement.ProcurementNotFound as error:
        raise HTTPException(404, str(error)) from error
    except procurement.ProcurementConflict as error:
        raise HTTPException(409, str(error)) from error
    except procurement.ProcurementInvalid as error:
        raise HTTPException(422, str(error)) from error


def create_procurement_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse the bearer dependency and transaction for every procurement read and write."""
    router = APIRouter(prefix="/purchase-orders")
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post("", response_model=OrderOut, status_code=201)
    def create(body: OrderCreate, context: Context):
        """Create a draft expectation using only credential-derived tenant and creator."""
        with procurement_errors():
            return procurement.create_order(
                context.connection,
                org_id=context.identity.organization_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    @router.get("", response_model=list[OrderOut])
    def list_orders(context: Context):
        """Return tenant-visible expectations."""
        return procurement.list_orders(context.connection)

    @router.get("/{po_id}", response_model=OrderOut)
    def get(po_id: UUID, context: Context):
        """Address an expectation by opaque UUID."""
        with procurement_errors():
            return procurement.get_order(context.connection, po_id)

    @router.patch("/{po_id}", response_model=OrderOut)
    def update(po_id: UUID, body: OrderUpdate, context: Context):
        """Edit only draft business metadata."""
        with procurement_errors():
            return procurement.update_order(
                context.connection, po_id, values=body.model_dump(exclude_unset=True)
            )

    @router.post("/{po_id}/issue", response_model=OrderOut)
    def issue(po_id: UUID, context: Context, body: Annotated[InputModel, Body()] = EMPTY_ISSUE):
        """Issue and freeze atomically; supplied authority fields are forbidden."""
        with procurement_errors():
            return procurement.issue_order(context.connection, po_id)

    @router.post("/{po_id}/lines", response_model=LineOut, status_code=201)
    def create_line(po_id: UUID, body: LineCreate, context: Context):
        """Create a draft root and snapshot its selected Item default UOM."""
        with procurement_errors():
            return procurement.create_line(
                context.connection,
                po_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    @router.get("/{po_id}/lines", response_model=list[LineOut])
    def history(po_id: UUID, context: Context):
        """Expose every version in pointer-derived root-to-leaf order."""
        with procurement_errors():
            return procurement.list_lines(context.connection, po_id)

    @router.patch("/{po_id}/lines/{line_id}", response_model=LineOut)
    def update_line(po_id: UUID, line_id: UUID, body: LineUpdate, context: Context):
        """Edit permitted draft values without changing lineage or UOM."""
        with procurement_errors():
            return procurement.update_line(
                context.connection, po_id, line_id, values=body.model_dump(exclude_unset=True)
            )

    @router.post("/{po_id}/lines/{line_id}/supersede", response_model=LineOut, status_code=201)
    def supersede(po_id: UUID, line_id: UUID, body: AmendmentCreate, context: Context):
        """Amend the exact selected active version, preserving the predecessor unchanged."""
        with procurement_errors():
            return procurement.create_line(
                context.connection,
                po_id,
                predecessor_id=line_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    return router
