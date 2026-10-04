"""Batch ingestion revalidates credentials and commits every operation separately."""

from collections.abc import Callable, Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials

from fleetops.api.assets import asset_errors
from fleetops.api.capture_schemas import CaptureBatch, OperationOut, ResolvedEntity
from fleetops.api.context import RequestContext
from fleetops.capture import resolver, service


def create_capture_router(
    authenticated: Callable[..., Iterator[RequestContext]], engine, bearer, storage
):
    router = APIRouter()
    Context = Annotated[RequestContext, Depends(authenticated)]
    Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

    @router.post("/capture/operations", response_model=list[OperationOut])
    def ingest(body: CaptureBatch, response: Response, credentials: Credentials):
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "Invalid credentials or session", headers={"WWW-Authenticate": "Bearer"}
            )
        response.headers["Cache-Control"] = "no-store"
        try:
            return service.batch(engine, credentials.credentials, body.operations, storage)
        except service.CredentialExpired as error:
            # Earlier independently committed outcomes remain durable; replay discovers
            # them after login. Expiry never confers authority on later operations.
            raise HTTPException(
                401, "Invalid credentials or session", headers={"WWW-Authenticate": "Bearer"}
            ) from error

    @router.get("/resolve/{identifier}", response_model=ResolvedEntity)
    def resolve(identifier: UUID, context: Context):
        with asset_errors():
            return resolver.resolve(context.connection, identifier)

    return router
