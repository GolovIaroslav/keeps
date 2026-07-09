"""Clipboard-backend-agnostic capture logic: kind detection, size cap, self-set guard."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

MIME_PLAIN = "text/plain"
MIME_HTML = "text/html"
MIME_IMAGE = "image/png"
MIME_URI_LIST = "text/uri-list"

DEFAULT_MAX_ITEM_MB = 10

# Mime types to fetch for each kind, in priority order (see PLAN.md §5 canon).
_MIMES_FOR_KIND = {
    "image": [MIME_IMAGE],
    "files": [MIME_URI_LIST, MIME_PLAIN],
    "html": [MIME_HTML, MIME_PLAIN],
    "text": [MIME_PLAIN],
}


def detect_kind(available: set[str]) -> str | None:
    """Pick a clip kind from the mime types offered by the clipboard."""
    if MIME_IMAGE in available:
        return "image"
    if MIME_URI_LIST in available:
        return "files"
    if MIME_HTML in available:
        return "html"
    if MIME_PLAIN in available:
        return "text"
    return None


def select_bundle(
    kind: str, available: set[str], reader: Callable[[str], bytes]
) -> dict[str, bytes]:
    """Read the mime types relevant to `kind` via `reader` (side-effecting, injectable)."""
    bundle = {}
    for mime in _MIMES_FOR_KIND[kind]:
        if mime in available:
            bundle[mime] = reader(mime)
    return bundle


def within_size_cap(mime_data: dict[str, bytes], max_item_mb: float) -> bool:
    max_bytes = int(max_item_mb * 1024 * 1024)
    return sum(len(data) for data in mime_data.values()) <= max_bytes


def build_bundle(
    available: set[str],
    reader: Callable[[str], bytes],
    max_item_mb: float = DEFAULT_MAX_ITEM_MB,
) -> tuple[str, dict[str, bytes]] | None:
    """Turn offered mime types into a (kind, mime_data) bundle ready for Store.add().

    Returns None if no known kind is offered, or the content exceeds max_item_mb.
    """
    kind = detect_kind(available)
    if kind is None:
        return None
    bundle = select_bundle(kind, available, reader)
    if not bundle:
        return None
    if not within_size_cap(bundle, max_item_mb):
        logger.debug("clip exceeds max_item_mb=%s, skipping", max_item_mb)
        return None
    return kind, bundle


class SelfSetGuard:
    """Skips the single clipboard-change event that follows our own clipboard write."""

    def __init__(self, window_seconds: float = 1.0) -> None:
        self._window_seconds = window_seconds
        self._deadline: float = 0.0

    def mark_self_set(self) -> None:
        self._deadline = time.monotonic() + self._window_seconds

    def consume_skip(self) -> bool:
        """Call once per observed change event; returns True if it should be ignored."""
        skip = self._deadline > 0.0 and time.monotonic() < self._deadline
        self._deadline = 0.0
        return skip
