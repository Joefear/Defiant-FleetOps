"""One Party can play several roles; vendor and manufacturer remain distinct (D7)."""

from enum import StrEnum


class PartyRole(StrEnum):
    VENDOR = "VENDOR"
    MANUFACTURER = "MANUFACTURER"
    CUSTOMER = "CUSTOMER"
    CARRIER = "CARRIER"
    SUBCONTRACTOR = "SUBCONTRACTOR"
    INTERNAL = "INTERNAL"
