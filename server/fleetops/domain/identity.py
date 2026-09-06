"""Slice 2 persistence operations; no asset, procurement, or operational workflow.

The trusted caller supplies the performer. HTTP request models intentionally have no
org_id or actor_id fields; both come from resolved authentication before reaching here.
"""

from uuid import UUID

from sqlalchemy import Connection, select
from uuid6 import uuid7

from fleetops.db.metadata import actors, parties, party_roles
from fleetops.domain.actor_types import ActorType
from fleetops.domain.party_roles import PartyRole


def create_actor(
    connection: Connection,
    *,
    org_id: UUID,
    performer_id: UUID,
    actor_type: ActorType,
    display_name: str,
):
    """A typed attribution identity does not imply a local login (D10)."""
    return (
        connection.execute(
            actors.insert()
            .values(
                id=uuid7(),
                org_id=org_id,
                type=actor_type.value,
                display_name=display_name,
                created_by_actor_id=performer_id,
            )
            .returning(actors)
        )
        .mappings()
        .one()
    )


def create_party(
    connection: Connection,
    *,
    org_id: UUID,
    performer_id: UUID,
    display_name: str,
    roles: set[PartyRole],
):
    """The party and its role set are written in the same transaction."""
    if not roles:
        raise ValueError("A party requires at least one role")
    party = (
        connection.execute(
            parties.insert()
            .values(
                id=uuid7(),
                org_id=org_id,
                display_name=display_name,
                created_by_actor_id=performer_id,
            )
            .returning(parties)
        )
        .mappings()
        .one()
    )
    connection.execute(
        party_roles.insert(),
        [
            {"id": uuid7(), "org_id": org_id, "party_id": party["id"], "role": role.value}
            for role in sorted(roles)
        ],
    )
    return {**party, "roles": sorted(roles)}


def list_parties(connection: Connection) -> list[dict]:
    """Read through RLS without a caller-selectable organization filter."""
    rows = connection.execute(
        select(parties, party_roles.c.role)
        .join(
            party_roles,
            (parties.c.org_id == party_roles.c.org_id) & (parties.c.id == party_roles.c.party_id),
        )
        .order_by(parties.c.id, party_roles.c.role)
    ).mappings()
    results = {}
    for row in rows:
        if row["id"] not in results:
            results[row["id"]] = {key: row[key] for key in parties.c.keys()}
            results[row["id"]]["roles"] = []
        results[row["id"]]["roles"].append(row["role"])
    return list(results.values())
