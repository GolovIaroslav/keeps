"""Clipboard-backend-agnostic capture logic: kind detection, size cap, self-set guard."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

MIME_PLAIN = "text/plain"
MIME_HTML = "text/html"
MIME_IMAGE = "image/png"
MIME_URI_LIST = "text/uri-list"

DEFAULT_MAX_ITEM_MB = 10
EXTRA_MIME_MAX_BYTES = 1024 * 1024

# Mime types to fetch for each kind, in priority order (see PLAN.md §5 canon).
_MIMES_FOR_KIND = {
    "image": [MIME_IMAGE],
    "files": [MIME_URI_LIST, MIME_PLAIN],
    "html": [MIME_HTML, MIME_PLAIN],
    "text": [MIME_PLAIN],
}

_REAL_FORMATTING_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "a",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "td",
        "th",
        "thead",
        "tbody",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)


def html_has_real_formatting(html_bytes: bytes) -> bool:
    """True if html contains tags beyond a trivial wrapper.

    This covers bold/italic/underline/link/list/table/heading tags.

    Deliberately does NOT count <pre>/<code>/<span>/<div>/<p>/<font> as "real"
    formatting -- these are exactly the trivial-wrapper tags browsers/chat-UI
    pages use to shell plain prose (e.g. `<html><body><pre>...</pre></body></html>`),
    which is the concrete real-world case this function exists to catch (see the
    PLAN.md item on capture/base.py::detect_kind()).
    """
    text = html_bytes.decode("utf-8", errors="replace")
    return any(
        tag.lower() in _REAL_FORMATTING_TAGS
        for tag in re.findall(r"<\s*([a-zA-Z][a-zA-Z0-9]*)", text)
    )


def detect_kind(available: set[str], html_bytes: bytes | None = None) -> str | None:
    """Pick a clip kind from the mime types offered by the clipboard."""
    if MIME_IMAGE in available:
        return "image"
    if MIME_URI_LIST in available:
        return "files"
    if MIME_HTML in available:
        if (
            html_bytes is not None
            and MIME_PLAIN in available
            and not html_has_real_formatting(html_bytes)
        ):
            return "text"
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
    *,
    store_all_formats: bool = False,
) -> tuple[str, dict[str, bytes]] | None:
    """Turn offered mime types into a (kind, mime_data) bundle ready for Store.add().

    Returns None if no known kind is offered, or the content exceeds max_item_mb.
    """
    cache: dict[str, bytes] = {}

    def read(mime: str) -> bytes:
        if mime not in cache:
            cache[mime] = reader(mime)
        return cache[mime]

    kind = detect_kind(available)
    if kind is None:
        return None
    if kind == "html":
        html_bytes = read(MIME_HTML)
        kind = detect_kind(available, html_bytes)
        if kind == "html":
            bundle = {MIME_HTML: html_bytes}
            if MIME_PLAIN in available:
                bundle[MIME_PLAIN] = read(MIME_PLAIN)
        else:
            bundle = select_bundle(kind, available, read)
    else:
        bundle = select_bundle(kind, available, read)
    if not bundle:
        return None
    if not within_size_cap(bundle, max_item_mb):
        logger.debug("clip exceeds max_item_mb=%s, skipping", max_item_mb)
        return None
    if store_all_formats:
        max_total_bytes = int(max_item_mb * 1024 * 1024)
        total_bytes = sum(len(value) for value in bundle.values())
        for mime in sorted(available - bundle.keys()):
            data = read(mime)
            if len(data) > EXTRA_MIME_MAX_BYTES:
                logger.debug("extra MIME %s exceeds the 1 MiB per-format cap", mime)
                continue
            if total_bytes + len(data) > max_total_bytes:
                logger.debug("extra MIME %s would exceed max_item_mb=%s", mime, max_item_mb)
                continue
            bundle[mime] = data
            total_bytes += len(data)
    return kind, bundle


def should_store(kind: str, store_html: bool, store_images: bool, store_files: bool) -> bool:
    """Whether a captured clip of this kind should be kept (PLAN.md §7 capture/* toggles)."""
    if kind == "html":
        return store_html
    if kind == "image":
        return store_images
    if kind == "files":
        return store_files
    return True


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
