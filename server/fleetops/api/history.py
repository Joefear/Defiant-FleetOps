"""Authenticated tenant-scoped final v0.1 reconstruction and health reads."""

from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from fleetops.api.assets import asset_errors
from fleetops.api.context import RequestContext
from fleetops.api.history_schemas import AssetHistoryOut, ReconciliationOut
from fleetops.domain.history import asset_history
from fleetops.domain.reconciliation import reconciliation_health


def create_history_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse bearer resolution/RLS; callers gain neither tenant selection nor SQL authority."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.get("/assets/{asset_id}/history", response_model=AssetHistoryOut)
    def history(asset_id: UUID, context: Context):
        with asset_errors():
            return asset_history(context.connection, asset_id)

    @router.get("/health/reconciliation", response_model=ReconciliationOut)
    def health(context: Context):
        return reconciliation_health(context.connection)

    return router
