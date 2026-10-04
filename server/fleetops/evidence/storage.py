"""D17 write-once storage, isolated by tenant and addressed by server-computed SHA-256.

The configured directory is trusted deployment storage: operators must prevent other
processes from replacing its directories. Reparse checks reject accidental redirection;
they do not claim to defeat a hostile administrator racing filesystem operations.
Publication uses an atomic hard link from a fully flushed private temporary file.
Readers therefore never observe a partially written final key, and a competing writer
cannot overwrite it. A failed database transaction may leave an unreferenced blob;
deleting such blobs requires a separately governed retention policy.
"""

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import UUID


class EvidenceStorageError(Exception):
    """Missing, corrupt or unavailable bytes; callers receive no host path details."""


@dataclass(frozen=True)
class StoredEvidence:
    """An immutable content reference, never a caller-selected filesystem path."""

    key: str
    sha256: str
    byte_size: int


class EvidenceStorage(Protocol):
    """Adapter contract: atomic write-once publication and verified retrieval."""

    def put(self, organization_id: UUID, content: bytes) -> StoredEvidence: ...

    def read(self, organization_id: UUID, reference: StoredEvidence) -> bytes: ...


class FilesystemEvidenceStorage:
    """Reference filesystem adapter; original filenames never participate in addressing."""

    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("Evidence storage root must be absolute")
        self.root = root
        self._check_path(root)

    @staticmethod
    def _check_path(path: Path) -> None:
        """Reject symlinks and Windows junctions along the complete configured path."""
        for part in (path, *path.parents):
            if part.is_symlink() or part.is_junction():
                raise EvidenceStorageError("Evidence storage path is redirected")

    def _path(self, organization_id: UUID, reference: StoredEvidence) -> Path:
        if (
            not isinstance(organization_id, UUID)
            or re.fullmatch(r"[0-9a-f]{64}", reference.sha256) is None
            or reference.byte_size < 0
            or reference.key != f"{organization_id}/{reference.sha256}"
        ):
            raise EvidenceStorageError("Invalid evidence storage reference")
        path = self.root / str(organization_id) / reference.sha256
        self._check_path(path)
        return path

    def read(self, organization_id: UUID, reference: StoredEvidence) -> bytes:
        """Read one bounded object and verify its recorded hash and size on every access."""
        path = self._path(organization_id, reference)
        try:
            with path.open("rb") as stream:
                # Reject inconsistent metadata before it controls allocation size.
                # The open handle binds this check to the same file we then hash.
                if os.fstat(stream.fileno()).st_size != reference.byte_size:
                    raise EvidenceStorageError("Evidence integrity check failed")
                content = stream.read(reference.byte_size + 1)
        except OSError as error:
            raise EvidenceStorageError("Evidence bytes are unavailable") from error
        if (
            len(content) != reference.byte_size
            or hashlib.sha256(content).hexdigest() != reference.sha256
        ):
            raise EvidenceStorageError("Evidence integrity check failed")
        return content

    def put(self, organization_id: UUID, content: bytes) -> StoredEvidence:
        """Publish once; duplicates verify the existing bytes rather than rewriting them."""
        digest = hashlib.sha256(content).hexdigest()
        reference = StoredEvidence(f"{organization_id}/{digest}", digest, len(content))
        path = self._path(organization_id, reference)
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._check_path(path)
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as f:
                temporary = Path(f.name)
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temporary, path)
                # POSIX directory fsync persists the new directory entry. Windows
                # publication uses the same flushed file handle and atomic hard link.
                if os.name != "nt":
                    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            except FileExistsError:
                if self.read(organization_id, reference) != content:
                    raise EvidenceStorageError("Existing evidence content differs") from None
        except OSError as error:
            raise EvidenceStorageError("Evidence storage is unavailable") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as error:
                    # Failed cleanup must preserve the storage error boundary too;
                    # a fully published object is retained for a verified retry.
                    raise EvidenceStorageError("Evidence storage cleanup failed") from error
        return reference
