"""Decode actual rendered artifacts; inspecting an input string is not a payload proof."""

import ast
import re
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pypdfium2
import pytest
import zxingcpp
from PIL import Image
from pydantic import ValidationError

from fleetops.api.label_schemas import TemplateCreate
from fleetops.labels.adapters.file import FileAdapter
from fleetops.labels.adapters.storage import ArtifactStore
from fleetops.labels.adapters.zpl import ZplAdapter
from fleetops.labels.render import render

IDENTITY = UUID("019a2000-0000-7000-8000-123456789abc")


def snapshot(symbology):
    return {
        "id": str(IDENTITY),
        "symbology": symbology,
        "human_fields": ["asset_tag", "description", "item_mpn"],
        "asset_tag": "REPRINT-THIS-TAG",
        "item_mpn": "PART-99",
        "description": "Human text ^XZ https://example.invalid/ must not enter the symbol",
    }


def decode(image):
    found = zxingcpp.read_barcodes(image)
    assert len(found) == 1
    return found[0]


@pytest.mark.parametrize("symbology", ["CODE128", "DATAMATRIX"])
@pytest.mark.parametrize("output_format", ["PNG", "PDF"])
def test_rendered_file_decodes_to_uuid_only(symbology, output_format, tmp_path):
    store = ArtifactStore(tmp_path)
    adapter = FileAdapter(store)
    label = render(snapshot(symbology))
    digest = adapter.publish(IDENTITY, IDENTITY, label, output_format)
    content = adapter.read(IDENTITY, IDENTITY, output_format, digest)
    if output_format == "PNG":
        with Image.open(BytesIO(content)) as image:
            found = decode(image)
    else:
        with pypdfium2.PdfDocument(content) as document:
            assert len(document) == 1
            page = document[0]
            bitmap = page.render(scale=300 / 72)
            try:
                found = decode(bitmap.to_pil())
            finally:
                bitmap.close()
                page.close()
    assert found.text == str(IDENTITY)
    assert found.bytes == str(IDENTITY).encode("ascii")
    assert found.format == (
        zxingcpp.BarcodeFormat.Code128
        if symbology == "CODE128"
        else zxingcpp.BarcodeFormat.DataMatrix
    )
    # Fixed PDF metadata and lossless pixels make the same persisted job retryable.
    assert adapter.publish(IDENTITY, IDENTITY, label, output_format) == digest


@pytest.mark.parametrize("symbology", ["CODE128", "DATAMATRIX"])
def test_protocol_adapter_raster_decodes_and_text_cannot_inject_commands(symbology, tmp_path):
    adapter = ZplAdapter(ArtifactStore(tmp_path))
    label = render(snapshot(symbology))
    digest = adapter.publish(IDENTITY, IDENTITY, label, "ZPL")
    output = adapter.read(IDENTITY, IDENTITY, "ZPL", digest).decode("ascii")
    match = re.fullmatch(
        r"\^XA\^PW(\d+)\^LL(\d+)\^FO0,0\^GFA,(\d+),(\d+),(\d+),([0-9A-F]+)\^FS\^XZ",
        output,
    )
    assert match
    width, height, count, total, row_bytes = map(int, match.groups()[:5])
    pixels = bytes(byte ^ 255 for byte in bytes.fromhex(match.group(6)))
    assert count == total == len(pixels) == row_bytes * height
    image = Image.frombytes("1", (width, height), pixels)
    assert decode(image).text == str(IDENTITY)
    assert output.count("^XZ") == 1 and "example.invalid" not in output


@pytest.mark.parametrize("field", ["asset_tag", "serial", "location", "url", "{id}:{asset_tag}"])
def test_template_rejects_every_nonidentity_barcode_field(field):
    with pytest.raises(ValidationError):
        TemplateCreate(
            name="Invalid", symbology="CODE128", human_fields=["id"], barcode_field=field
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"human_fields": ["serial"]},
        {"human_fields": ["id", "id"]},
        {"human_fields": []},
        {"entity_type": "LOT"},
        {"payload": "{asset_tag}"},
    ],
)
def test_template_rejects_unsupported_or_composite_layout(changes):
    with pytest.raises(ValidationError):
        TemplateCreate.model_validate(
            {"name": "Invalid", "symbology": "DATAMATRIX", "human_fields": ["id"], **changes}
        )


def test_printer_protocol_imports_are_confined_to_adapters():
    """Inspect imports across production modules rather than trusting a package name."""
    root = Path(__file__).resolve().parents[2] / "fleetops"
    assert root.is_dir()
    for module in root.rglob("*.py"):
        if module.is_relative_to(root / "labels" / "adapters"):
            continue
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or "", *(alias.name for alias in node.names)]
            assert not any("zebra" in name.lower() or "zpl" in name.lower() for name in names), (
                module,
                names,
            )
