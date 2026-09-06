"""Five authenticated space routes sharing the established transaction-local RLS context."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from fleetops.api.context import RequestContext
from fleetops.api.space_schemas import FacilityCreate, FacilityOut, LocationCreate, LocationOut
from fleetops.domain import space


@contextmanager
def space_errors():
    """Translate domain failures without leaking tenant data or committing failed writes."""
    try:
        yield
    except space.SpaceNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (space.SpaceConflict, space.SpaceHierarchyInvalid) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except space.SpaceInvalid as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def create_space_router(authenticated: Callable[..., Iterator[RequestContext]]) -> APIRouter:
    """Reuse bearer authentication for reads and writes; no update/delete surface is opened."""
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]

    @router.post("/facilities", response_model=FacilityOut, status_code=201)
    def add_facility(body: FacilityCreate, context: Context):
        """Derive organization and creator from the authenticated session."""
        with space_errors():
            return space.create_facility(
                context.connection,
                org_id=context.identity.organization_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    @router.get("/facilities", response_model=list[FacilityOut])
    def list_facilities(context: Context):
        """Return only facilities visible to this request's RLS transaction."""
        return space.list_facilities(context.connection)

    @router.post("/locations", response_model=LocationOut, status_code=201)
    def add_location(body: LocationCreate, context: Context):
        """Record a space beneath a database-enforced tenant/facility parent."""
        with space_errors():
            return space.create_location(
                context.connection,
                org_id=context.identity.organization_id,
                performer_id=context.identity.actor_id,
                values=body.model_dump(),
            )

    @router.get("/locations", response_model=list[LocationOut])
    def list_locations(context: Context):
        """List visible spaces without imposing future lifecycle semantics."""
        return space.list_locations(context.connection)

    @router.get("/locations/{location_id}/path", response_model=list[LocationOut])
    def location_path(location_id: UUID, context: Context):
        """Return complete persisted ancestry or fail without exposing a partial chain."""
        with space_errors():
            return space.location_path(context.connection, location_id)

    return router
