"""D10 location vocabulary; persisted as TEXT with a database CHECK."""

from enum import StrEnum


class LocationKind(StrEnum):
    """Kinds describe space, without introducing occupancy or movement semantics."""

    SITE = "SITE"
    ROOM = "ROOM"
    RACK = "RACK"
    BIN = "BIN"
    STATION = "STATION"
    DOCK = "DOCK"
    VEHICLE = "VEHICLE"
    OTHER = "OTHER"
