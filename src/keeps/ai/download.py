"""Streaming download + sha256 verification for AI model weights (PLAN.md §9.1).

Pure logic, injectable `opener` for tests -- no Qt here. The caller (ui/settings.py)
runs this inside a QRunnable so the daemon's UI thread never blocks on network I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import IO
from urllib.request import urlopen

CHUNK_SIZE = 1024 * 256


class ChecksumError(Exception):
    def __init__(self, path: Path, expected: str, actual: str) -> None:
        super().__init__(f"{path}: expected sha256 {expected}, got {actual}")
        self.path = path
        self.expected = expected
        self.actual = actual


def download_file(
    url: str,
    dest: Path,
    expected_sha256: str,
    opener: Callable[[str], IO[bytes]] = urlopen,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Download `url` to `dest`, verifying sha256 after the fact.

    On a checksum mismatch the partial/corrupt file is removed and
    ChecksumError is raised -- callers must not treat a mismatched file as
    downloaded. Writes to a `.part` sibling first and renames on success, so a
    crash mid-download never leaves a file that `models.is_downloaded()` would
    accept.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part_path = dest.with_suffix(dest.suffix + ".part")

    hasher = hashlib.sha256()
    total = 0
    with opener(url) as response, open(part_path, "wb") as out:
        content_length = int(getattr(response, "length", 0) or 0)
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            hasher.update(chunk)
            total += len(chunk)
            if progress_cb is not None:
                progress_cb(total, content_length)

    digest = hasher.hexdigest()
    if digest != expected_sha256:
        part_path.unlink(missing_ok=True)
        raise ChecksumError(dest, expected_sha256, digest)

    part_path.replace(dest)
