"""Actor kinds describe attribution, not a user/permission hierarchy (D10)."""

from enum import StrEnum


class ActorType(StrEnum):
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    DEVICE = "DEVICE"
    INTEGRATION = "INTEGRATION"
