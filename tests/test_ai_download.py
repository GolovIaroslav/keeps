import hashlib
import io

import pytest

from keeps.ai.download import ChecksumError, download_file


class _FakeResponse(io.BytesIO):
    """Mimics the subset of http.client.HTTPResponse download_file relies on."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.length = len(data)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(data: bytes):
    return lambda url: _FakeResponse(data)


def test_download_file_writes_content_on_matching_checksum(tmp_path):
    data = b"hello world" * 1000
    digest = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "model.onnx"

    download_file("http://example/model.onnx", dest, digest, opener=_opener(data))

    assert dest.read_bytes() == data
    assert not dest.with_suffix(".onnx.part").exists()


def test_download_file_reports_progress(tmp_path):
    data = b"x" * (300 * 1024)  # > one CHUNK_SIZE, so progress_cb fires more than once
    digest = hashlib.sha256(data).hexdigest()
    dest = tmp_path / "model.onnx"
    calls = []

    download_file(
        "http://example/model.onnx",
        dest,
        digest,
        opener=_opener(data),
        progress_cb=lambda done, total: calls.append((done, total)),
    )

    assert len(calls) >= 2
    assert calls[-1][0] == len(data)


def test_download_file_raises_and_cleans_up_on_checksum_mismatch(tmp_path):
    data = b"corrupted payload"
    dest = tmp_path / "model.onnx"

    with pytest.raises(ChecksumError):
        download_file("http://example/model.onnx", dest, "0" * 64, opener=_opener(data))

    assert not dest.exists()
    assert not dest.with_suffix(".onnx.part").exists()
