"""Bounded Slice 12 label vocabulary; encoded identity is never a template field."""

from enum import StrEnum


class Symbology(StrEnum):
    """Both supported symbols carry the same canonical FleetOps UUID string."""

    CODE128 = "CODE128"
    DATAMATRIX = "DATAMATRIX"


class HumanField(StrEnum):
    """Descriptive fields affect visible text only, never machine-readable content."""

    ID = "id"
    ASSET_TAG = "asset_tag"
    DESCRIPTION = "description"
    ITEM_MPN = "item_mpn"


class OutputFormat(StrEnum):
    """Artifact formats are selected independently of the domain entity."""

    PNG = "PNG"
    PDF = "PDF"
    ZPL = "ZPL"


class PrintStatus(StrEnum):
    """SUCCEEDED means an output artifact exists, not that a physical printer ran."""

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
