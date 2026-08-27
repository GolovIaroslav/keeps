"""Clipboard-backend-agnostic capture logic: kind detection, size cap, self-set guard."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable
from html.parser import HTMLParser

from keeps.text_encoding import decode_unicode_escapes, normalize_plain_text

__all__ = [
    "decode_unicode_escapes",
    "extract_text_from_html",
    "find_plain_text_mime",
    "has_plain_text",
    "normalize_plain_text",
]

logger = logging.getLogger(__name__)

MIME_PLAIN = "text/plain"
MIME_HTML = "text/html"
MIME_IMAGE = "image/png"
MIME_URI_LIST = "text/uri-list"

# All mime types that represent plain text, in priority order:
PLAIN_MIME_CANDIDATES = (
    "text/plain;charset=utf-8",
    "text/plain;charset=UTF-8",
    "text/plain; charset=utf-8",
    "text/plain; charset=UTF-8",
    "UTF8_STRING",
    "text/plain",
    "TEXT",
    "STRING",
)

_CHARSET_RE = re.compile(
    r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\"'\s]+)", re.IGNORECASE
)

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


def find_plain_text_mime(available: set[str]) -> str | None:
    """Find the best matching plain-text MIME type from available formats."""
    for candidate in PLAIN_MIME_CANDIDATES[:5]:
        if candidate in available:
            return candidate

    # Any declared charset is stronger evidence than an unqualified
    # text/plain fallback. This matters when producers advertise both.
    for mime in sorted(available):
        if mime.lower().startswith("text/plain") and _CHARSET_RE.search(mime):
            return mime

    for candidate in (MIME_PLAIN, "TEXT", "STRING"):
        if candidate in available:
            return candidate

    for mime in sorted(available):
        norm = mime.lower().replace(" ", "")
        if norm in ("text/plain;charset=utf-8", "utf8_string", "text/plain"):
            return mime
    for mime in sorted(available):
        if mime.lower().startswith("text/plain"):
            return mime
    return None


def has_plain_text(available: set[str]) -> bool:
    """True if any supported plain-text MIME type is present in available."""
    return find_plain_text_mime(available) is not None


def _plain_text_encoding_hint(mime: str) -> str | None:
    if mime == "UTF8_STRING":
        return "utf-8"
    if mime == "STRING":
        return "iso-8859-1"
    match = _CHARSET_RE.search(mime)
    return match.group(1) if match is not None else None


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.result: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style", "head"):
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in (
            "p",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "tr",
        ):
            self.result.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in ("script", "style", "head") and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.result.append(data)


def extract_text_from_html(html_bytes: bytes) -> str:
    """Extract clean plain text from HTML bytes, stripping tags and normalizing Unicode."""
    html_text = html_bytes.decode("utf-8", errors="replace")
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
        text = "".join(parser.result)
    except Exception:
        text = re.sub(r"<[^>]+>", "", html_text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n", "\n", text).strip()
    return unicodedata.normalize("NFC", text)


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
            and has_plain_text(available)
            and not html_has_real_formatting(html_bytes)
        ):
            return "text"
        return "html"
    if has_plain_text(available):
        return "text"
    return None


def select_bundle(
    kind: str, available: set[str], reader: Callable[[str], bytes]
) -> dict[str, bytes]:
    """Read the mime types relevant to `kind` via `reader` (side-effecting, injectable)."""
    bundle = {}
    if kind == "text":
        plain_mime = find_plain_text_mime(available)
        if plain_mime is not None:
            bundle[MIME_PLAIN] = reader(plain_mime)
        return bundle
    if kind == "html":
        if MIME_HTML in available:
            bundle[MIME_HTML] = reader(MIME_HTML)
        plain_mime = find_plain_text_mime(available)
        if plain_mime is not None:
            bundle[MIME_PLAIN] = reader(plain_mime)
        return bundle
    for mime in _MIMES_FOR_KIND[kind]:
        if mime in available:
            if mime == MIME_PLAIN:
                plain_mime = find_plain_text_mime(available)
                if plain_mime is not None:
                    bundle[MIME_PLAIN] = reader(plain_mime)
            else:
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
            if (
                mime == MIME_PLAIN
                or mime in PLAIN_MIME_CANDIDATES
                or mime.lower().startswith("text/plain")
            ):
                cache[mime] = normalize_plain_text(
                    cache[mime], _plain_text_encoding_hint(mime)
                )
        return cache[mime]

    kind = detect_kind(available)
    if kind is None:
        return None
    if kind == "html":
        html_bytes = read(MIME_HTML)
        kind = detect_kind(available, html_bytes)
        if kind == "html":
            bundle = {MIME_HTML: html_bytes}
            plain_mime = find_plain_text_mime(available)
            if plain_mime is not None:
                bundle[MIME_PLAIN] = read(plain_mime)
            elif html_bytes:
                fallback = extract_text_from_html(html_bytes)
                if fallback:
                    bundle[MIME_PLAIN] = fallback.encode("utf-8")
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
