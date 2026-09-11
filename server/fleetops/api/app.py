"""Local HUMAN authentication with tenant-scoped catalog, space, and Asset routes."""

from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from uuid6 import uuid7

from fleetops.api.assets import create_asset_router
from fleetops.api.catalog import create_catalog_router
from fleetops.api.context import RequestContext
from fleetops.api.procurement import create_procurement_router
from fleetops.api.schemas import (
    ActorCreate,
    ActorOut,
    IdentityOut,
    Login,
    PartyCreate,
    PartyOut,
    SessionOut,
)
from fleetops.api.space import create_space_router
from fleetops.auth import (
    authenticate_password,
    issue_token,
    resolve_identity,
)
from fleetops.db.metadata import actors
from fleetops.db.session import create_authenticator_engine, create_runtime_engine
from fleetops.db.tenancy import set_credential_context, set_organization
from fleetops.domain.identity import create_actor, create_party, list_parties
from fleetops.settings import Settings

bearer = HTTPBearer(auto_error=False)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Invalid credentials or session",
        headers={"WWW-Authenticate": "Bearer"},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Own and dispose independent ordinary-runtime and login-only connection pools."""
    settings = settings or Settings.from_environment()
    engine = create_runtime_engine(settings)
    authenticator_engine = create_authenticator_engine(settings)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            engine.dispose()
            authenticator_engine.dispose()

    app = FastAPI(title="Defiant FleetOps", lifespan=lifespan)
    app.state.engine = engine
    app.state.authenticator_engine = authenticator_engine

    def authenticated(
        response: Response,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Iterator[RequestContext]:
        """Resolve a credential first, then keep tenant work inside this transaction.

        Commit, rollback, exceptions, and pool return all end the SET LOCAL context.
        No request body, header, query value, or configured login org participates in
        authenticated tenant selection.
        """
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise unauthorized()
        with engine.begin() as connection:
            identity = resolve_identity(connection, credentials.credentials)
            if identity is None:
                raise unauthorized()
            set_organization(connection, identity.organization_id)
            set_credential_context(connection, identity.token_digest)
            response.headers["Cache-Control"] = "no-store"
            yield RequestContext(connection, identity)

    Context = Annotated[RequestContext, Depends(authenticated)]

    @app.post("/auth/login", response_model=SessionOut)
    def login(body: Login, response: Response):
        # ADR-002 permits trusted single-org configuration only on this intentionally
        # unauthenticated path. ADR-006 isolates issuance from ordinary runtime SQL.
        with authenticator_engine.begin() as connection:
            set_organization(connection, settings.organization_id)
            user = authenticate_password(
                connection, body.username, body.password.get_secret_value()
            )
            if user is None:
                raise unauthorized()
            raw_token, digest = issue_token()
            # Expiry is derived from the database clock, not the API host's: created_at
            # defaults to the server timestamp, and ck_sessions_expiration must never fail
            # because two machines disagree about what time it is.
            now = connection.execute(text("SELECT statement_timestamp()")).scalar_one()
            expires_at = now + timedelta(seconds=settings.session_seconds)
            connection.execute(
                text("SELECT fleetops.issue_session(:user, :session, :digest, :expiry)"),
                {"user": user[0], "session": uuid7(), "digest": digest, "expiry": expires_at},
            )
        response.headers["Cache-Control"] = "no-store"
        return SessionOut(access_token=raw_token, expires_at=expires_at)

    @app.get("/auth/me", response_model=IdentityOut)
    def me(context: Context):
        return IdentityOut(
            org_id=context.identity.organization_id,
            user_id=context.identity.user_id,
            actor_id=context.identity.actor_id,
        )

    @app.post("/auth/logout", status_code=204)
    def logout(context: Context) -> Response:
        context.connection.execute(text("SELECT fleetops.revoke_current_session()"))
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/actors", response_model=ActorOut, status_code=201)
    def add_actor(body: ActorCreate, context: Context):
        return create_actor(
            context.connection,
            org_id=context.identity.organization_id,
            performer_id=context.identity.actor_id,
            actor_type=body.type,
            display_name=body.display_name,
        )

    @app.get("/actors", response_model=list[ActorOut])
    def get_actors(context: Context):
        return context.connection.execute(select(actors).order_by(actors.c.id)).mappings().all()

    @app.post("/parties", response_model=PartyOut, status_code=201)
    def add_party(body: PartyCreate, context: Context):
        return create_party(
            context.connection,
            org_id=context.identity.organization_id,
            performer_id=context.identity.actor_id,
            display_name=body.display_name,
            roles=body.roles,
        )

    @app.get("/parties", response_model=list[PartyOut])
    def get_parties(context: Context):
        return list_parties(context.connection)

    app.include_router(create_catalog_router(authenticated))
    app.include_router(create_space_router(authenticated))
    app.include_router(create_asset_router(authenticated))
    app.include_router(create_procurement_router(authenticated))
    return app
