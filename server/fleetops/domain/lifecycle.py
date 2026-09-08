"""Python-owned lifecycle graph from Build Handoff v1.2 Section 3 (D3, D8).

ON_HOLD remembers its entry state through authoritative transition history. Neither a
client-supplied previous state nor a location/assignment projection can choose its exit.
"""

from enum import StrEnum


class AssetState(StrEnum):
    """The canonical v0.1 state set; PostgreSQL stores TEXT with generated CHECKs."""

    RECEIVED = "RECEIVED"
    IN_STOCK = "IN_STOCK"
    CONFIGURING = "CONFIGURING"
    READY = "READY"
    DEPLOYED = "DEPLOYED"
    ON_HOLD = "ON_HOLD"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"
    RETIRED = "RETIRED"


S = AssetState
LIFECYCLE = {
    S.RECEIVED: frozenset({S.IN_STOCK, S.ON_HOLD}),
    S.IN_STOCK: frozenset({S.CONFIGURING, S.ON_HOLD, S.RETIRED}),
    S.CONFIGURING: frozenset({S.READY, S.ON_HOLD}),
    S.READY: frozenset({S.DEPLOYED, S.IN_STOCK, S.ON_HOLD}),
    S.DEPLOYED: frozenset({S.ON_HOLD, S.OUT_OF_SERVICE, S.READY}),
    S.ON_HOLD: frozenset({S.OUT_OF_SERVICE}),
    S.OUT_OF_SERVICE: frozenset({S.CONFIGURING, S.RETIRED}),
    S.RETIRED: frozenset(),
}


class LifecycleInvalid(ValueError):
    """The requested edge is outside the lifecycle graph or evidence boundary."""


def validate_edge(from_state: AssetState, to_state: AssetState, *, previous_state=None) -> None:
    """Validate graph legality only; legal retirement still needs executable evidence."""
    allowed = LIFECYCLE[from_state]
    if from_state == S.ON_HOLD and previous_state is not None:
        allowed = allowed | {previous_state}
    if to_state not in allowed:
        raise LifecycleInvalid("Illegal lifecycle transition")
