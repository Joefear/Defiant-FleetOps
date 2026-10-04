"""D17 write-once publication under failure, duplicate writers and hostile key inputs."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from fleetops.evidence.storage import (
    EvidenceStorageError,
    FilesystemEvidenceStorage,
    StoredEvidence,
)


def test_parallel_publication_returns_one_complete_immutable_object(evidence_root):
    storage = FilesystemEvidenceStorage(evidence_root)
    org = uuid4()
    content = b"complete evidence\n" * 10000
    barrier = Barrier(8)

    def publish(_):
        barrier.wait(timeout=10)
        return storage.put(org, content)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(publish, range(8)))
    assert len(set(results)) == 1
    reference = results[0]
    assert storage.read(org, reference) == content
    path = evidence_root / reference.key
    before = path.stat().st_mtime_ns
    assert storage.put(org, content) == reference
    assert path.stat().st_mtime_ns == before
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("bad_key", ["../escape", "/escape", "C:/escape", "other/hash", ""])
def test_key_cannot_choose_a_path(evidence_root, bad_key):
    storage = FilesystemEvidenceStorage(evidence_root)
    with pytest.raises(EvidenceStorageError):
        storage.read(uuid4(), StoredEvidence(bad_key, "a" * 64, 1))
    assert not evidence_root.exists()


def test_corrupt_existing_key_is_never_overwritten(evidence_root):
    storage = FilesystemEvidenceStorage(evidence_root)
    org = uuid4()
    reference = storage.put(org, b"original")
    path = evidence_root / reference.key
    path.write_bytes(b"damaged!")
    with pytest.raises(EvidenceStorageError, match="integrity"):
        storage.read(org, reference)
    with pytest.raises(EvidenceStorageError, match="integrity"):
        storage.put(org, b"original")
    assert path.read_bytes() == b"damaged!"
    assert list(path.parent.iterdir()) == [path]


def test_oversized_size_claim_fails_before_it_controls_read_allocation(evidence_root):
    storage = FilesystemEvidenceStorage(evidence_root)
    org = uuid4()
    reference = storage.put(org, b"real bytes")
    forged = StoredEvidence(reference.key, reference.sha256, 2**63 - 1)
    with pytest.raises(EvidenceStorageError, match="integrity"):
        storage.read(org, forged)
    assert storage.read(org, reference) == b"real bytes"


def test_failed_publication_leaves_no_final_or_temporary_object(evidence_root, monkeypatch):
    storage = FilesystemEvidenceStorage(evidence_root)

    def fail(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr("fleetops.evidence.storage.os.link", fail)
    with pytest.raises(EvidenceStorageError):
        storage.put(uuid4(), b"no partial publication")
    assert not [p for p in evidence_root.rglob("*") if p.is_file()]


def test_identical_content_is_physically_tenant_scoped(evidence_root):
    storage = FilesystemEvidenceStorage(evidence_root)
    a, b = uuid4(), uuid4()
    left, right = storage.put(a, b"same"), storage.put(b, b"same")
    assert left.sha256 == right.sha256 == hashlib.sha256(b"same").hexdigest()
    assert left.key != right.key
    with pytest.raises(EvidenceStorageError):
        storage.read(a, right)


def test_cleanup_failure_retains_complete_object_and_has_bounded_error(evidence_root, monkeypatch):
    storage = FilesystemEvidenceStorage(evidence_root)
    org = uuid4()

    def fail(*args, **kwargs):
        raise PermissionError("host path detail")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(EvidenceStorageError, match="^Evidence storage cleanup failed$"):
            storage.put(org, b"published before cleanup")
    reference = storage.put(org, b"published before cleanup")
    assert storage.read(org, reference) == b"published before cleanup"


def test_relative_root_rejects():
    with pytest.raises(ValueError, match="absolute"):
        FilesystemEvidenceStorage(Path("relative-evidence"))
