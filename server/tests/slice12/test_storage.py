"""Atomic local artifact publication remains safe under I/O failure and competing writers."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from fleetops.labels.adapters.storage import ArtifactStore, OutputUnavailable


def test_parallel_publish_never_exposes_partial_files(tmp_path):
    store = ArtifactStore(tmp_path)
    org, job = uuid4(), uuid4()
    content = b"complete label bytes" * 10000
    barrier = Barrier(4)

    def publish(_):
        barrier.wait(timeout=5)
        return store.publish(org, job, "png", content)

    with ThreadPoolExecutor(max_workers=4) as executor:
        hashes = list(executor.map(publish, range(4)))
    assert hashes == [hashlib.sha256(content).hexdigest()] * 4
    assert store.read(org, job, "png", hashes[0]) == content
    assert list((tmp_path / str(org)).iterdir()) == [store.path(org, job, "png")]


def test_different_retry_never_overwrites_original_output(tmp_path):
    store = ArtifactStore(tmp_path)
    org, job = uuid4(), uuid4()
    original = store.publish(org, job, "png", b"original")
    with pytest.raises(OutputUnavailable):
        store.publish(org, job, "png", b"different")
    assert store.read(org, job, "png", original) == b"original"


def test_failed_publication_removes_only_its_temporary_file(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)

    def fail(*args):
        raise OSError("private host detail")

    monkeypatch.setattr("fleetops.labels.adapters.storage.os.link", fail)
    with pytest.raises(OutputUnavailable, match="^Output storage is unavailable$"):
        store.publish(uuid4(), uuid4(), "png", b"partial")
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_cleanup_failure_has_bounded_error_and_retry_reuses_complete_file(tmp_path, monkeypatch):
    store = ArtifactStore(tmp_path)
    org, job = uuid4(), uuid4()

    def fail(*args, **kwargs):
        raise PermissionError("private host detail")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(OutputUnavailable, match="^Output cleanup failed$"):
            store.publish(org, job, "png", b"complete")
    digest = store.publish(org, job, "png", b"complete")
    assert store.read(org, job, "png", digest) == b"complete"


def test_relative_root_and_unregistered_extension_reject(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        ArtifactStore(Path("relative"))
    store = ArtifactStore(tmp_path)
    with pytest.raises(OutputUnavailable):
        store.publish(uuid4(), uuid4(), "../escape", b"invalid")
    assert list(tmp_path.iterdir()) == []


def test_oversized_artifact_rejects_before_file_output(tmp_path):
    store = ArtifactStore(tmp_path)
    with pytest.raises(OutputUnavailable):
        store.publish(uuid4(), uuid4(), "png", b"x" * (4 * 1024 * 1024 + 1))
    assert list(tmp_path.iterdir()) == []
