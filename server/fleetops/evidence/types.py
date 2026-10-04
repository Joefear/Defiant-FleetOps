"""D17 capture vocabulary and the bounded v0.1 evidence-link surface."""

from enum import StrEnum


class EvidenceSource(StrEnum):
    """What was captured, independently of the role it plays in a record."""

    PHOTO = "PHOTO"
    DOCUMENT = "DOCUMENT"
    SCAN = "SCAN"
    REPORT = "REPORT"
    CERTIFICATE = "CERTIFICATE"
    OTHER = "OTHER"


class EvidenceRole(StrEnum):
    """A link records purpose; a filename or media type never grants authority."""

    RECEIVING_EVIDENCE = "RECEIVING_EVIDENCE"
    DISPOSAL_EVIDENCE = "DISPOSAL_EVIDENCE"
    CONFIG_EVIDENCE = "CONFIG_EVIDENCE"
    EXCEPTION_EVIDENCE = "EXCEPTION_EVIDENCE"
    OTHER = "OTHER"


class EvidenceEntity(StrEnum):
    """Existing records that can carry evidence in the opened attachment slice."""

    ASSET = "ASSET"
    RECEIPT = "RECEIPT"
    RECEIPT_LINE = "RECEIPT_LINE"
    ASSET_CONFIGURATION = "ASSET_CONFIGURATION"
    RECEIVING_EXCEPTION = "RECEIVING_EXCEPTION"
