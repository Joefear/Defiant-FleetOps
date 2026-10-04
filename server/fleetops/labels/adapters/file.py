"""Reference PNG/PDF adapter; output generation does not claim physical printing."""

from io import BytesIO
from time import gmtime

from fleetops.labels.adapters.storage import ArtifactStore
from fleetops.labels.render import png_bytes


class FileAdapter:
    """Lossless symbols and stable PDF metadata allow exact retries of one job."""

    name = "FILE"

    def __init__(self, store: ArtifactStore):
        self.store = store

    def publish(self, organization, job_id, label, output_format):
        if output_format == "PNG":
            content = png_bytes(label)
        elif output_format == "PDF":
            buffer = BytesIO()
            # Monochrome PDF uses lossless CCITT encoding. Fixed metadata prevents
            # a retry's clock from manufacturing different bytes for the same job.
            label.image.convert("1").save(
                buffer,
                format="PDF",
                resolution=300,
                creationDate=gmtime(946684800),
                modDate=gmtime(946684800),
                title="FleetOps label",
            )
            content = buffer.getvalue()
        else:
            raise ValueError("Unsupported file output")
        return self.store.publish(organization, job_id, output_format.lower(), content)

    def read(self, organization, job_id, output_format, expected_hash):
        return self.store.read(organization, job_id, output_format.lower(), expected_hash)
