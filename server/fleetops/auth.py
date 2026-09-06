"""Local HUMAN passwords and opaque bearer credentials under ADR-002.

Argon2id deliberately spends CPU/memory on low-entropy human passwords. Bearer tokens
already have 256 bits of random source entropy; their lookup key is SHA-256, avoiding a
password KDF on every authenticated request. Neither raw secret is persisted.
"""

import hashlib
import secrets
from dataclasses import dataclass, field
from uuid import UUID

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Connection, select, text

from fleetops.db.metadata import actors, users

TOKEN_BYTES = 32
PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)
# Unknown users still pay for a password verification. This random dummy credential is
# process-local and is never inserted into the users table or exposed to callers.
_DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(TOKEN_BYTES))


@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Trusted credential resolution; the digest remains internal and out of reprs."""

    organization_id: UUID
    user_id: UUID
    actor_id: UUID
    token_digest: bytes = field(repr=False)


def hash_password(password: str) -> str:
    """Create a salted Argon2id hash without persisting or echoing the password."""
    if not password:
        raise ValueError("Password must not be empty")
    return PASSWORD_HASHER.hash(password)


def token_digest(raw_token: str) -> bytes:
    """Derive the fixed one-way lookup key for a high-entropy bearer credential."""
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def issue_token() -> tuple[str, bytes]:
    """Return the raw token once to the caller; only its digest belongs in a database."""
    raw_token = secrets.token_urlsafe(TOKEN_BYTES)
    return raw_token, token_digest(raw_token)


def authenticate_password(
    connection: Connection, username: str, password: str
) -> tuple[UUID, UUID] | None:
    """Ordinary RLS lookup in the configured org; failed authentication writes nothing."""
    row = connection.execute(
        select(
            users.c.id,
            users.c.actor_id,
            users.c.password_hash,
            users.c.active.label("user_active"),
            actors.c.active.label("actor_active"),
        )
        .join(
            actors,
            (actors.c.org_id == users.c.org_id)
            & (actors.c.id == users.c.actor_id)
            & (actors.c.type == "HUMAN"),
        )
        .where(users.c.username == username)
    ).first()
    try:
        correct = PASSWORD_HASHER.verify(row.password_hash if row else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        correct = False
    if row is None or not correct or not row.user_active or not row.actor_active:
        return None
    return row.id, row.actor_id


def resolve_identity(connection: Connection, raw_token: str) -> AuthenticatedIdentity | None:
    """Call only the accepted exact-digest resolver before tenant context exists."""
    # Issued tokens are 43 characters. Anything far longer is not a credential, so it
    # is refused before hashing rather than letting a caller spend our CPU on garbage.
    if not raw_token or len(raw_token) > 1024:
        return None
    digest = token_digest(raw_token)
    row = connection.execute(
        text("SELECT org_id, user_id, actor_id FROM fleetops.resolve_session(:digest)"),
        {"digest": digest},
    ).first()
    if row is None:
        return None
    return AuthenticatedIdentity(row.org_id, row.user_id, row.actor_id, digest)
