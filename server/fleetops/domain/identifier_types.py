"""Identifier labels never replace an Asset's permanent FleetOps UUID (D1, D2)."""

from enum import StrEnum


class IdentifierType(StrEnum):
    """Readable values and unreadable markers use the same five identifier kinds."""

    MANUFACTURER_SERIAL = "MANUFACTURER_SERIAL"
    PCB_SERIAL = "PCB_SERIAL"
    MAC = "MAC"
    IMEI = "IMEI"
    OTHER = "OTHER"
