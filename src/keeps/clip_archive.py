"""Portable, gzip-compressed clip archives used by the export/import tools."""

from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass

FORMAT = "keeps.clip-archive"
VERSION = 1

_CANONICAL_MIME = {
    "text": "text/plain",
    "image": "image/png",
    "files": "text/uri-list",
}
_VALID_KINDS = frozenset({"text", "html", "image", "files"})


@dataclass(frozen=True)
class ArchiveClip:
    """A clip detached from a particular Keeps database and its numeric ID."""

    kind: str
    mime_data: dict[str, bytes]
    pinned: bool = False
    alias: str | None = None


def encode_archive(clips: list[ArchiveClip]) -> bytes:
    """Encode clips as a deterministic gzip JSON document with base64 data."""
    document = {
        "format": FORMAT,
        "version": VERSION,
        "clips": [
            {
                "kind": clip.kind,
                "mime_data": {
                    mime: base64.b64encode(data).decode("ascii")
                    for mime, data in sorted(clip.mime_data.items())
                },
                "pinned": clip.pinned,
                "alias": clip.alias,
            }
            for clip in clips
        ],
    }
    encoded = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return gzip.compress(encoded.encode("utf-8"), mtime=0)


def decode_archive(payload: bytes) -> list[ArchiveClip]:
    """Decode and validate a user-selected archive before changing the store."""
    try:
        document = json.loads(gzip.decompress(payload).decode("utf-8"))
    except (EOFError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Not a valid Keeps archive.") from exc
    if (
        not isinstance(document, dict)
        or document.get("format") != FORMAT
        or document.get("version") != VERSION
        or not isinstance(document.get("clips"), list)
    ):
        raise ValueError("Unsupported Keeps archive format.")
    return [_decode_clip(raw_clip) for raw_clip in document["clips"]]


def _decode_clip(raw_clip: object) -> ArchiveClip:
    if not isinstance(raw_clip, dict):
        raise ValueError("Archive contains an invalid clip.")
    kind = raw_clip.get("kind")
    raw_mime_data = raw_clip.get("mime_data")
    pinned = raw_clip.get("pinned", False)
    alias = raw_clip.get("alias")
    if (
        kind not in _VALID_KINDS
        or not isinstance(raw_mime_data, dict)
        or not isinstance(pinned, bool)
        or alias is not None
        and not isinstance(alias, str)
    ):
        raise ValueError("Archive contains an invalid clip.")
    mime_data: dict[str, bytes] = {}
    for mime, encoded in raw_mime_data.items():
        if not isinstance(mime, str) or not mime or not isinstance(encoded, str):
            raise ValueError("Archive contains an invalid MIME format.")
        try:
            mime_data[mime] = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Archive contains invalid base64 data.") from exc
    if kind == "html":
        valid = "text/plain" in mime_data or "text/html" in mime_data
    else:
        valid = _CANONICAL_MIME[kind] in mime_data
    if not valid:
        raise ValueError("Archive clip is missing its canonical MIME format.")
    return ArchiveClip(kind, mime_data, pinned=pinned, alias=alias)
