"""Typed Asset correction routes; no generic caller-selected correction graph."""

from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from fleetops.api.asset_schemas import CustodyOut, MovementOut, OwnershipOut, TransitionOut
from fleetops.api.assets import asset_errors
from fleetops.api.assignment_schemas import AssignmentEventOut
from fleetops.api.context import RequestContext
from fleetops.api.correction_schemas import (
    AssignmentCorrection,
    CustodyCorrection,
    MovementCorrection,
    OwnershipCorrection,
    TransitionCorrection,
)
from fleetops.domain import corrections


def create_correction_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse the credential-resolved transaction for all pair and projection writes."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post(
        "/assets/{asset_id}/transitions/{root_id}/corrections",
        response_model=TransitionOut,
        status_code=201,
    )
    def transition(asset_id: UUID, root_id: UUID, body: TransitionCorrection, context: Context):
        """Correct an ordinary lifecycle edge using its effective pre-root state."""
        with asset_errors():
            return corrections.correct_transition(
                context.connection, asset_id, root_id, values=body.model_dump()
            )

    @router.post(
        "/assets/{asset_id}/movements/{root_id}/corrections",
        response_model=MovementOut,
        status_code=201,
    )
    def movement(asset_id: UUID, root_id: UUID, body: MovementCorrection, context: Context):
        """Correct a recorded movement without inventing a physical return."""
        with asset_errors():
            return corrections.correct_movement(
                context.connection, asset_id, root_id, values=body.model_dump()
            )

    @router.post(
        "/assets/{asset_id}/custody-changes/{root_id}/corrections",
        response_model=CustodyOut,
        status_code=201,
    )
    def custody(asset_id: UUID, root_id: UUID, body: CustodyCorrection, context: Context):
        """Correct custody independently of title and assignment."""
        with asset_errors():
            return corrections.correct_custody(
                context.connection, asset_id, root_id, values=body.model_dump()
            )

    @router.post(
        "/assets/{asset_id}/ownership-changes/{root_id}/corrections",
        response_model=OwnershipOut,
        status_code=201,
    )
    def ownership(asset_id: UUID, root_id: UUID, body: OwnershipCorrection, context: Context):
        """Correct the recorded owner while preserving all original facts."""
        with asset_errors():
            return corrections.correct_ownership(
                context.connection, asset_id, root_id, values=body.model_dump()
            )

    @router.post(
        "/assets/{asset_id}/assignments/{root_id}/corrections",
        response_model=AssignmentEventOut,
        status_code=201,
    )
    def assignment(asset_id: UUID, root_id: UUID, body: AssignmentCorrection, context: Context):
        """Correct an assignment assertion and its establishing-event projection."""
        with asset_errors():
            return corrections.correct_assignment(
                context.connection, asset_id, root_id, values=body.model_dump()
            )

    return router
