"""PO states describe procurement expectation, never receiving or payment progress."""

from enum import StrEnum


class PurchaseOrderStatus(StrEnum):
    """The handoff vocabulary; only DRAFT to ISSUED is currently executable."""

    DRAFT = "DRAFT"
    ISSUED = "ISSUED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
