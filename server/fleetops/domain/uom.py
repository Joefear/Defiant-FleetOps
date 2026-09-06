"""Catalog default units; packaging belongs to later stock-unit concepts (D13)."""

from enum import StrEnum


class UnitOfMeasure(StrEnum):
    """Bounded Slice 3 set shared by validation and the runtime SQL CHECK."""

    EA = "EA"
    M = "M"
    MM = "MM"
    CM = "CM"
    IN = "IN"
    FT = "FT"
    G = "G"
    MG = "MG"
    KG = "KG"
    ML = "ML"
    L = "L"
