"""Vendor-neutral raster rendering with a single non-configurable identity payload."""

from dataclasses import dataclass
from io import BytesIO
from uuid import UUID

import zxingcpp
from PIL import Image, ImageDraw, ImageFont

from fleetops.labels.types import HumanField, Symbology


@dataclass(frozen=True)
class RenderedLabel:
    """The barcode image and human-readable text are already composed before dispatch."""

    image: Image.Image
    payload: str


def render(snapshot: dict) -> RenderedLabel:
    """D14: derive the payload exclusively from identity, never template interpolation.

    Integer module scaling preserves quiet zones and avoids interpolation artifacts.
    Human fields are wrapped on separate lines and never overlap the barcode.
    """
    payload = str(UUID(snapshot["id"]))
    symbology = Symbology(snapshot["symbology"])
    barcode = zxingcpp.create_barcode(
        payload,
        zxingcpp.BarcodeFormat.Code128
        if symbology == Symbology.CODE128
        else zxingcpp.BarcodeFormat.DataMatrix,
    )
    symbol = Image.fromarray(barcode.to_image(scale=3, add_hrt=False, add_quiet_zones=True))
    width = max(1200, symbol.width + 48)
    fields = [HumanField(field) for field in snapshot["human_fields"]]
    captions = {
        "id": "FleetOps ID",
        "asset_tag": "Asset tag",
        "description": "Description",
        "item_mpn": "Part",
    }
    font = ImageFont.load_default(size=22)
    lines = []
    for field in fields:
        value = snapshot.get(field.value) or ""
        # Visible text can describe the thing; it does not get to rename identity.
        for source_line in (captions[field.value] + ": " + value).splitlines():
            # A character-count limit clips wide glyphs; measure actual pixels instead.
            current = ""
            for character in source_line:
                if current and font.getlength(current + character) > width - 48:
                    lines.append(current)
                    current = ""
                current += character
            lines.append(current)
    image = Image.new("L", (width, symbol.height + 64 + 30 * len(lines)), 255)
    image.paste(symbol, ((width - symbol.width) // 2, 24))
    drawing = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        drawing.text((24, symbol.height + 48 + index * 30), line, fill=0, font=font)
    return RenderedLabel(image, payload)


def png_bytes(label: RenderedLabel) -> bytes:
    """Use a lossless file; a barcode is a particularly poor place for JPEG."""
    output = BytesIO()
    label.image.save(output, format="PNG")
    return output.getvalue()
