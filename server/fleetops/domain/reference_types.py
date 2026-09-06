"""Only existing catalog/reference targets are opened in Slice 3."""

from enum import StrEnum


class ReferenceEntityType(StrEnum):
    """Extending this set later requires a target registry entry and a CHECK migration."""

    ITEM = "ITEM"
    PARTY = "PARTY"
