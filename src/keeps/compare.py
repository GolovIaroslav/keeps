"""Small, Qt-free helpers for comparing the textual contents of two clips."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

_TEXTUAL_MIMES = (
    ("text/plain", ".txt"),
    ("text/html", ".html"),
    ("text/uri-list", ".txt"),
)
_DIFF_TOOLS = ("meld", "kompare", "kdiff3")


def comparison_payload(
    left: dict[str, bytes], right: dict[str, bytes]
) -> tuple[str, bytes, bytes] | None:
    """Return one common textual representation, or None if none exists."""
    for mime, suffix in _TEXTUAL_MIMES:
        if mime in left and mime in right:
            return suffix, left[mime], right[mime]
    return None


def diff_command(configured: str, which: Callable[[str], str | None]) -> list[str] | None:
    """Choose a configured diff command or the first installed supported tool."""
    configured = configured.strip()
    if configured:
        try:
            command = shlex.split(configured)
        except ValueError:
            return None
        return command or None
    for tool in _DIFF_TOOLS:
        if which(tool):
            return [tool]
    return None


def write_comparison_pair(
    directory: Path, suffix: str, left: bytes, right: bytes
) -> tuple[Path, Path]:
    """Write the two operands into a private temporary directory."""
    directory.mkdir(parents=True, exist_ok=True)
    left_path = directory / f"left{suffix}"
    right_path = directory / f"right{suffix}"
    left_path.write_bytes(left)
    right_path.write_bytes(right)
    return left_path, right_path
