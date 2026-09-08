"""Authenticated Asset surface; receipt-owned creation and identifier capture stay deferred."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from fleetops.api.asset_schemas import (
    AssetOut,
    AssetPatch,
    IdentifierOut,
    StateDiscrepancy,
    TransitionOut,
    TransitionRequest,
)
from fleetops.api.context import RequestContext
from fleetops.domain import assets
from fleetops.domain.lifecycle import LifecycleInvalid


@contextmanager
def asset_errors():
    """Expose stable failures while the shared request dependency rolls back all writes."""
    try:
        yield
    except assets.AssetNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except assets.AssetConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except (assets.AssetInvalid, LifecycleInvalid) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_asset_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """All seven routes use the existing bearer session and transaction-local tenant context."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.get("/assets", response_model=list[AssetOut])
    def list_assets(context: Context):
        """List only the current tenant's Assets."""
        return assets.list_assets(context.connection)

    @router.get("/assets/{asset_id}", response_model=AssetOut)
    def get_asset(asset_id: UUID, context: Context):
        """Resolve permanent UUID identity through RLS."""
        with asset_errors():
            return assets.get_asset(context.connection, asset_id)

    @router.patch("/assets/{asset_id}", response_model=AssetOut)
    def patch_asset(asset_id: UUID, body: AssetPatch, context: Context):
        """Attribute descriptive changes to the authenticated actor and database clock."""
        with asset_errors():
            return assets.patch_asset(
                context.connection,
                asset_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(exclude_unset=True),
            )

    @router.get("/assets/{asset_id}/identifiers", response_model=list[IdentifierOut])
    def identifiers(asset_id: UUID, context: Context):
        """Read capture evidence without introducing a manual identifier-write workflow."""
        with asset_errors():
            return assets.list_identifiers(context.connection, asset_id)

    @router.get("/assets/{asset_id}/transitions", response_model=list[TransitionOut])
    def transitions(asset_id: UUID, context: Context):
        """Return authoritative result-version order, independent of event clocks."""
        with asset_errors():
            return assets.list_transitions(context.connection, asset_id)

    @router.post("/assets/{asset_id}/transitions", response_model=TransitionOut, status_code=201)
    def transition(asset_id: UUID, body: TransitionRequest, context: Context):
        """Validate lifecycle legality before invoking atomic history/projection admission."""
        with asset_errors():
            return assets.transition_asset(
                context.connection,
                asset_id,
                values=body.model_dump(),
            )

    @router.get("/health/assets/reconciliation", response_model=list[StateDiscrepancy])
    def reconciliation(context: Context):
        """Report state disagreements in this tenant; health reads never repair history."""
        return assets.reconcile_state(context.connection)

    return router
