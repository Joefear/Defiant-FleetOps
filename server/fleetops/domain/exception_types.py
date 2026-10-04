"""Logical Exception vocabulary; receiving keeps its narrower derived classifications."""

from enum import StrEnum

from fleetops.domain.receiving_types import ReceivingExceptionType

# Reuse every receiving token, including AMR-001's UOM_MISMATCH. The additional
# workflow tokens have no producer in Slice 10; materials remain reserved only.
ExceptionType = StrEnum(
    "ExceptionType",
    {
        **{kind.name: kind.value for kind in ReceivingExceptionType},
        "GENERAL": "GENERAL",
        "SYNC_CONFLICT": "SYNC_CONFLICT",
        "MISSING_LOT": "MISSING_LOT",
        "BALANCE_NEGATIVE": "BALANCE_NEGATIVE",
    },
)


class ExceptionSeverity(StrEnum):
    """No governed severity assessment exists yet; never invent historical risk."""

    UNSPECIFIED = "UNSPECIFIED"


class ExceptionEntityType(StrEnum):
    """Primary receiving context; all additional typed references remain intact."""

    RECEIPT = "RECEIPT"
    RECEIPT_LINE = "RECEIPT_LINE"
