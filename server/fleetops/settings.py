"""Explicit server configuration; there is no request-selected or default tenant."""

import os
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.engine import URL, make_url


@dataclass(frozen=True)
class Settings:
    database_url: URL = field(repr=False)
    organization_id: UUID
    session_seconds: int = 43200

    def __post_init__(self) -> None:
        if (
            self.database_url.drivername != "postgresql+psycopg"
            or self.database_url.username != "fleetops_app"
        ):
            raise ValueError("Runtime requires postgresql+psycopg and fleetops_app credentials")
        if not 1 <= self.session_seconds <= 86400:
            raise ValueError("Session lifetime must be between 1 second and 24 hours")

    @classmethod
    def from_environment(cls) -> "Settings":
        """Missing trusted configuration is an error, never a fallback to the seeded org."""
        return cls(
            database_url=make_url(os.environ["FLEETOPS_APP_URL"]),
            organization_id=UUID(os.environ["FLEETOPS_ORG_ID"]),
            session_seconds=int(os.environ.get("FLEETOPS_SESSION_SECONDS", "43200")),
        )
