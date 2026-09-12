"""The bounded Slice 9 vocabulary; PostgreSQL stores text with CHECK constraints."""

from enum import StrEnum


class ReceiptCondition(StrEnum):
    """One physical condition per line; UNKNOWN is not GOOD."""

    GOOD = "GOOD"
    DAMAGED = "DAMAGED"
    OPENED = "OPENED"
    UNKNOWN = "UNKNOWN"


class ReceivingExceptionType(StrEnum):
    """Existing D5/AMR-001 categories, without resolution state or future materials types."""

    SHORT = "SHORT"
    OVER = "OVER"
    SUBSTITUTION = "SUBSTITUTION"
    DAMAGED = "DAMAGED"
    OPENED = "OPENED"
    SERIAL_UNREADABLE = "SERIAL_UNREADABLE"
    SERIAL_MISMATCH = "SERIAL_MISMATCH"
    UNEXPECTED_ITEM = "UNEXPECTED_ITEM"
    QUANTITY_VARIANCE = "QUANTITY_VARIANCE"
    UOM_MISMATCH = "UOM_MISMATCH"
