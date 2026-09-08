"""Create the first local user after migrations, using trusted configuration only.

Passwords do not belong in migrations: migration history is permanent and widely read.
This separate command uses deployment authority, creates one initial user, and never
updates an existing credential. UUID uniqueness and a transaction lock protect retries.
"""

import os
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from uuid6 import uuid7

from fleetops.api.schemas import Login
from fleetops.auth import hash_password
from fleetops.db.metadata import actors, users
from fleetops.db.seed import ACTOR_ID, ORGANIZATION_ID
from fleetops.db.session import create_deployment_engine
from fleetops.db.tenancy import set_organization


class BootstrapRefused(ValueError):
    """No initial user was created and no existing credential was overwritten."""


def bootstrap_user(engine: Engine, *, organization_id: UUID, username: str, password: str) -> UUID:
    """Serialize initial-user creation, validate the seeded HUMAN, and insert atomically."""
    if organization_id != ORGANIZATION_ID:
        raise BootstrapRefused("Bootstrap requires the migration-seeded organization")
    if not password:
        raise BootstrapRefused("A nonempty initial password is required")
    # Validation does not normalize passwords or quietly rename an existing account.
    validated = Login(username=username, password=password)
    try:
        with engine.begin() as connection:
            set_organization(connection, organization_id)
            # This lock serializes only cooperating bootstrap commands. Unique org/actor
            # and org/username constraints also reject a concurrent ordinary INSERT.
            connection.execute(text("SELECT pg_advisory_xact_lock(1867342112)"))
            if (
                connection.execute(
                    select(users.c.id).where(users.c.org_id == organization_id).limit(1)
                ).first()
                is not None
            ):
                raise BootstrapRefused("An initial user already exists; refusing overwrite")
            actor = connection.execute(
                select(actors.c.id).where(
                    actors.c.id == ACTOR_ID,
                    actors.c.org_id == organization_id,
                    actors.c.type == "HUMAN",
                    actors.c.active,
                )
            ).first()
            if actor is None:
                raise BootstrapRefused("The seeded active HUMAN actor is unavailable")
            user_id = uuid7()
            connection.execute(
                users.insert().values(
                    id=user_id,
                    org_id=organization_id,
                    actor_id=ACTOR_ID,
                    username=validated.username,
                    password_hash=hash_password(password),
                )
            )
            return user_id
    except IntegrityError:
        raise BootstrapRefused("Conflicting initial user; refusing overwrite") from None


def bootstrap_from_environment(environment: Mapping[str, str] | None = None) -> UUID:
    """Keep real credentials in the caller's environment; never print them or their hash."""
    environment = os.environ if environment is None else environment
    organization_id = UUID(environment["FLEETOPS_ORG_ID"])
    engine = create_deployment_engine(make_url(environment["FLEETOPS_MIGRATOR_URL"]))
    try:
        return bootstrap_user(
            engine,
            organization_id=organization_id,
            username=environment["FLEETOPS_BOOTSTRAP_USERNAME"],
            password=environment["FLEETOPS_BOOTSTRAP_PASSWORD"],
        )
    finally:
        engine.dispose()


def main() -> None:
    try:
        user_id = bootstrap_from_environment()
    except (KeyError, ValueError):
        # Generic CLI failure avoids leaking credentials through exception formatting.
        raise SystemExit(
            "Initial user was not created; check configuration and existing users"
        ) from None
    print(f"Created initial user {user_id}")


if __name__ == "__main__":
    main()
