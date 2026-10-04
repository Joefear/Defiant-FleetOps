"""ZPL raster output stays isolated here; no network or printer socket is opened."""

from fleetops.labels.adapters.storage import ArtifactStore


class ZplAdapter:
    """Rasterize the complete label so descriptive text cannot inject printer commands."""

    name = "ZPL"

    def __init__(self, store: ArtifactStore):
        self.store = store

    def publish(self, organization, job_id, label, output_format):
        if output_format != "ZPL":
            raise ValueError("Unsupported printer output")
        # ZPL graphic bits use 1 for black, while Pillow mode 1 uses 1 for white.
        image = label.image.convert("1")
        raster = bytes(byte ^ 0xFF for byte in image.tobytes())
        row_bytes = (image.width + 7) // 8
        text = (
            f"^XA^PW{image.width}^LL{image.height}^FO0,0"
            f"^GFA,{len(raster)},{len(raster)},{row_bytes},{raster.hex().upper()}^FS^XZ"
        )
        return self.store.publish(organization, job_id, "zpl", text.encode("ascii"))

    def read(self, organization, job_id, output_format, expected_hash):
        return self.store.read(organization, job_id, "zpl", expected_hash)
