"""The transaction and identity established by the shared authentication dependency."""

from dataclasses import dataclass

from sqlalchemy import Connection

from fleetops.auth import AuthenticatedIdentity


@dataclass(frozen=True)
class RequestContext:
    """Tenant work uses this connection for the lifetime of one authenticated transaction."""

    connection: Connection
    identity: AuthenticatedIdentity
