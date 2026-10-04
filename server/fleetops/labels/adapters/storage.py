"""Job-addressed local artifacts with no client-controlled filesystem path."""

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from uuid import UUID


class OutputUnavailable(Exception):
    """Bounded adapter failure; paths and operating-system details stay private."""


class ArtifactStore:
    """Atomic no-overwrite publication makes a persisted job safely retryable.

    The configured directory is administrator-owned. Reparse points are refused;
    this does not claim protection from a privileged host administrator.
    """

    def __init__(self, root: Path):
        if not root.is_absolute():
            raise ValueError("Label output root must be absolute")
        self.root = root
        self._check(root)

    @staticmethod
    def _check(path):
        for candidate in (path, *path.parents):
            if candidate.is_symlink() or candidate.is_junction():
                raise OutputUnavailable("Output storage is unavailable")

    def path(self, organization: UUID, job_id: UUID, extension: str) -> Path:
        """Only server-generated UUID components and registered formats form filenames."""
        if extension not in {"png", "pdf", "zpl"}:
            raise OutputUnavailable("Unsupported output format")
        target = (
            self.root / str(UUID(str(organization))) / (str(UUID(str(job_id))) + "." + extension)
        )
        self._check(target)
        return target

    def publish(self, organization, job_id, extension, content):
        """The winner publishes once; a retry must match the complete existing bytes."""
        if len(content) > 4 * 1024 * 1024:
            raise OutputUnavailable("Output artifact exceeds the configured boundary")
        target = self.path(organization, job_id, extension)
        temporary = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            self._check(target)
            descriptor, temporary = tempfile.mkstemp(prefix=".label-", dir=target.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                if self._read(target) != content:
                    raise OutputUnavailable("Existing output does not match the job") from None
            self._check(target)
            return hashlib.sha256(content).hexdigest()
        except OSError as error:
            raise OutputUnavailable("Output storage is unavailable") from error
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError as error:
                    raise OutputUnavailable("Output cleanup failed") from error

    def _read(self, path):
        self._check(path)
        try:
            if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 4 * 1024 * 1024:
                raise OutputUnavailable("Invalid output artifact")
            return path.read_bytes()
        except OSError as error:
            raise OutputUnavailable("Output storage is unavailable") from error

    def read(self, organization, job_id, extension, expected_hash):
        """A successful job never licenses serving swapped or corrupted bytes."""
        data = self._read(self.path(organization, job_id, extension))
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise OutputUnavailable("Output artifact failed integrity validation")
        return data
