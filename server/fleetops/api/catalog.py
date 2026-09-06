"""Authenticated catalog routes; external-reference lookup always returns a collection."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response

from fleetops.api.catalog_schemas import (
    ExternalKind,
    ExternalSystem,
    ExternalValue,
    ItemCreate,
    ItemOut,
    ItemUpdate,
    ReferenceCreate,
    ReferenceOut,
)
from fleetops.api.context import RequestContext
from fleetops.domain import catalog
from fleetops.domain.reference_types import ReferenceEntityType


@contextmanager
def catalog_errors():
    """Keep constraint details private and let failures roll back the shared transaction."""
    try:
        yield
    except catalog.CatalogNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except catalog.CatalogConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except catalog.CatalogInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_catalog_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse the existing bearer resolver and tenant transaction for every Slice 3 route."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post("/items", response_model=ItemOut, status_code=201)
    def add_item(body: ItemCreate, context: Context):
        """Assign tenant and performer from the session; the body supplies catalog facts."""
        with catalog_errors():
            return catalog.create_item(
                context.connection,
                org_id=context.identity.organization_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    @router.get("/items", response_model=list[ItemOut])
    def list_items(context: Context):
        """Include inactive entries so deactivation preserves catalog discoverability."""
        return catalog.list_items(context.connection)

    @router.get("/items/{item_id}", response_model=ItemOut)
    def get_item(item_id: UUID, context: Context):
        """Resolve only the opaque UUID, with the same 404 for missing and invisible items."""
        with catalog_errors():
            return catalog.get_item(context.connection, item_id)

    @router.patch("/items/{item_id}", response_model=ItemOut)
    def update_item(item_id: UUID, body: ItemUpdate, context: Context):
        """Apply explicit descriptive/default edits while preserving omitted catalog facts."""
        with catalog_errors():
            return catalog.update_item(
                context.connection,
                item_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(exclude_unset=True),
            )

    @router.delete("/items/{item_id}", status_code=204)
    def deactivate_item(item_id: UUID, context: Context):
        """Preserve permanent identity and attachments while marking the entry inactive."""
        # Deactivation preserves stable targets and reference attachments. No runtime
        # DELETE grant or physical deletion is needed for this catalog operation.
        with catalog_errors():
            catalog.update_item(
                context.connection,
                item_id,
                performer_id=context.identity.actor_id,
                values={"active": False},
            )
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @router.post("/external-references", response_model=ReferenceOut, status_code=201)
    def attach_reference(body: ReferenceCreate, context: Context):
        """Require an explicit visible ITEM/PARTY target before recording its reference."""
        with catalog_errors():
            return catalog.attach_reference(
                context.connection,
                org_id=context.identity.organization_id,
                performer_id=context.identity.actor_id,
                entity_type=body.entity_type,
                entity_id=body.entity_id,
                values=body.model_dump(exclude={"entity_type", "entity_id"}),
            )

    @router.get("/external-references", response_model=list[ReferenceOut])
    def list_references(entity_type: ReferenceEntityType, entity_id: UUID, context: Context):
        """Verify target visibility before exposing attachments to that explicit ID."""
        with catalog_errors():
            return catalog.list_references(context.connection, entity_type, entity_id)

    @router.get("/external-references/search", response_model=list[ReferenceOut])
    def search_references(
        system: ExternalSystem,
        reference_type: ExternalKind,
        external_value: ExternalValue,
        context: Context,
    ):
        """Return every matching target ID without treating result cardinality as identity."""
        return catalog.search_references(
            context.connection,
            system=system,
            reference_type=reference_type,
            external_value=external_value,
        )

    return router
