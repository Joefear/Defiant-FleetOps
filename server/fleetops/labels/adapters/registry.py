"""Choose adapters by a bounded server registry, never a client import path."""

from typing import Protocol

from fleetops.labels.adapters.file import FileAdapter
from fleetops.labels.adapters.storage import ArtifactStore
from fleetops.labels.adapters.zpl import ZplAdapter


class PrinterAdapter(Protocol):
    """Core orchestration depends only on publishing and reading one job artifact."""

    name: str

    def publish(self, organization, job_id, label, output_format) -> str: ...
    def read(self, organization, job_id, output_format, expected_hash) -> bytes: ...


def adapter_for(output_format) -> str:
    """Resolve a format to its registered implementation without filesystem input."""
    return "ZPL" if output_format == "ZPL" else "FILE"


def registry(root):
    """Missing trusted configuration disables dispatch, while queue records still exist."""
    if root is None:
        return {}
    store = ArtifactStore(root)
    return {"FILE": FileAdapter(store), "ZPL": ZplAdapter(store)}
