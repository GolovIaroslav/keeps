"""Pure in-memory full-content search index (PLAN.md Ф12)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from keeps.store import normalize

CONTENT_LIMIT_BYTES = 10 * 1024
SEARCH_HISTORY_LIMIT = 20


class MatchReason(StrEnum):
    EXACT = "exact"
    OCR = "ocr"


def remember_query(
    history: list[str], query: str, limit: int = SEARCH_HISTORY_LIMIT
) -> list[str]:
    query = query.strip()
    if not query:
        return list(history)
    query_key = normalize(query)
    previous = [item for item in history if normalize(item) != query_key]
    return [query, *previous][:limit]


@dataclass(frozen=True)
class _Document:
    content: str
    ocr: str


def _decode_limited(data: bytes) -> str:
    return data[:CONTENT_LIMIT_BYTES].decode("utf-8", errors="replace")


def _file_names(uri_list: bytes) -> str:
    names = []
    for raw_uri in _decode_limited(uri_list).splitlines():
        path = unquote(urlparse(raw_uri).path)
        name = PurePosixPath(path).name
        if name:
            names.append(name)
    return "\n".join(names)


def _content_for(kind: str, mime_data: dict[str, bytes]) -> str:
    if kind in ("text", "html"):
        data = mime_data.get("text/plain") or mime_data.get("text/html", b"")
        return _decode_limited(data)
    if kind == "files":
        return _file_names(mime_data.get("text/uri-list", b""))
    return ""


class SearchIndex:
    def __init__(self) -> None:
        self._documents: dict[int, _Document] = {}

    def upsert(
        self,
        clip_id: int,
        kind: str,
        mime_data: dict[str, bytes],
        ocr_text: str | None = None,
    ) -> None:
        self._documents[clip_id] = _Document(
            content=normalize(_content_for(kind, mime_data)),
            ocr=normalize(ocr_text or ""),
        )

    def remove(self, clip_id: int) -> None:
        self._documents.pop(clip_id, None)

    def update_ocr(self, clip_id: int, ocr_text: str) -> None:
        document = self._documents.get(clip_id)
        if document is not None:
            self._documents[clip_id] = _Document(
                content=document.content,
                ocr=normalize(ocr_text),
            )

    def search(self, query: str) -> dict[int, MatchReason]:
        terms = [normalize(term) for term in query.split() if term]
        if not terms:
            return {}

        matches = {}
        for clip_id, document in self._documents.items():
            content_hits = [term in document.content for term in terms]
            if all(content_hits):
                matches[clip_id] = MatchReason.EXACT
                continue
            if all(hit or term in document.ocr for hit, term in zip(content_hits, terms)):
                matches[clip_id] = MatchReason.OCR
        return matches
