"""SQLite-backed clip history: schema, dedup/move-to-top, trim, search."""

from __future__ import annotations

import hashlib
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
  id           INTEGER PRIMARY KEY,
  created_at   INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL,
  kind         TEXT    NOT NULL,
  preview      TEXT    NOT NULL,
  hash         TEXT    NOT NULL UNIQUE,
  pinned       INTEGER NOT NULL DEFAULT 0,
  use_count    INTEGER NOT NULL DEFAULT 0,
  ocr_text     TEXT
);
CREATE TABLE IF NOT EXISTS clip_data (
  clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  mime    TEXT    NOT NULL,
  data    BLOB    NOT NULL,
  PRIMARY KEY (clip_id, mime)
);
CREATE TABLE IF NOT EXISTS thumbs (
  clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
  png     BLOB NOT NULL
);
"""

PREVIEW_MAX_CHARS = 300


@dataclass(frozen=True)
class Clip:
    id: int
    created_at: int
    last_used_at: int
    kind: str
    preview: str
    hash: str
    pinned: bool
    use_count: int
    ocr_text: str | None


def normalize(text: str) -> str:
    """Single source of truth for case-insensitive string comparison/search."""
    return text.casefold()


def _canonical_bytes(kind: str, mime_data: dict[str, bytes]) -> bytes:
    """Bytes used to compute the dedup hash, per kind (see PLAN.md §5)."""
    if kind == "text":
        return mime_data["text/plain"]
    if kind == "image":
        return mime_data["image/png"]
    if kind == "files":
        return mime_data["text/uri-list"]
    if kind == "html":
        return mime_data.get("text/plain", mime_data.get("text/html", b""))
    raise ValueError(f"unknown kind: {kind}")


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    """Read width/height from a PNG IHDR chunk without image libraries."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def build_preview(kind: str, mime_data: dict[str, bytes]) -> str:
    if kind == "text":
        text = mime_data["text/plain"].decode("utf-8", errors="replace")
        return text[:PREVIEW_MAX_CHARS]
    if kind == "html":
        text = mime_data.get("text/plain", b"").decode("utf-8", errors="replace")
        return text[:PREVIEW_MAX_CHARS]
    if kind == "image":
        dims = _png_dimensions(mime_data["image/png"])
        return f"[image {dims[0]}x{dims[1]}]" if dims else "[image]"
    if kind == "files":
        names = mime_data["text/uri-list"].decode("utf-8", errors="replace").splitlines()
        return ", ".join(names)
    raise ValueError(f"unknown kind: {kind}")


class Store:
    def __init__(self, db_path: Path | str, max_items: int = 500):
        self.max_items = max_items
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def add(self, kind: str, mime_data: dict[str, bytes]) -> int:
        """Insert a new clip, or move an existing duplicate to the top."""
        content_hash = hashlib.sha256(_canonical_bytes(kind, mime_data)).hexdigest()
        now = int(time.time() * 1000)

        existing = self._conn.execute(
            "SELECT id FROM clips WHERE hash = ?", (content_hash,)
        ).fetchone()
        if existing is not None:
            clip_id = existing["id"]
            self.touch(clip_id)
            return clip_id

        preview = build_preview(kind, mime_data)
        cur = self._conn.execute(
            "INSERT INTO clips (created_at, last_used_at, kind, preview, hash, use_count) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (now, now, kind, preview, content_hash),
        )
        clip_id = cur.lastrowid
        self._conn.executemany(
            "INSERT INTO clip_data (clip_id, mime, data) VALUES (?, ?, ?)",
            [(clip_id, mime, data) for mime, data in mime_data.items()],
        )
        self._conn.commit()
        self.trim()
        return clip_id

    def touch(self, clip_id: int) -> None:
        """Move a clip to the top of the list (used-item invariant)."""
        now = int(time.time() * 1000)
        self._conn.execute(
            "UPDATE clips SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            (now, clip_id),
        )
        self._conn.commit()

    def delete(self, clip_id: int) -> None:
        self._conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        self._conn.commit()

    def update_content(self, clip_id: int, mime_data: dict[str, bytes]) -> int:
        """Replace a clip's content in place (used by external-editor Ctrl+E).

        Kind is preserved. If the edited content now matches another existing
        clip's hash, the two are merged (this clip is dropped, the other is
        touched) per the dedup invariant. Returns the resulting clip id.
        """
        row = self._conn.execute(
            "SELECT kind FROM clips WHERE id = ?", (clip_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no such clip: {clip_id}")
        kind = row["kind"]
        new_hash = hashlib.sha256(_canonical_bytes(kind, mime_data)).hexdigest()

        existing = self._conn.execute(
            "SELECT id FROM clips WHERE hash = ? AND id != ?", (new_hash, clip_id)
        ).fetchone()
        if existing is not None:
            self.delete(clip_id)
            self.touch(existing["id"])
            return existing["id"]

        preview = build_preview(kind, mime_data)
        now = int(time.time() * 1000)
        self._conn.execute(
            "UPDATE clips SET preview = ?, hash = ?, last_used_at = ? WHERE id = ?",
            (preview, new_hash, now, clip_id),
        )
        self._conn.execute("DELETE FROM clip_data WHERE clip_id = ?", (clip_id,))
        self._conn.executemany(
            "INSERT INTO clip_data (clip_id, mime, data) VALUES (?, ?, ?)",
            [(clip_id, mime, data) for mime, data in mime_data.items()],
        )
        self._conn.commit()
        return clip_id

    def set_pinned(self, clip_id: int, pinned: bool) -> None:
        self._conn.execute(
            "UPDATE clips SET pinned = ? WHERE id = ?", (int(pinned), clip_id)
        )
        self._conn.commit()

    def trim(self) -> None:
        """Delete the oldest unpinned clips beyond max_items."""
        self._conn.execute(
            "DELETE FROM clips WHERE pinned = 0 AND id IN ("
            "  SELECT id FROM clips WHERE pinned = 0"
            "  ORDER BY last_used_at DESC, id DESC"
            "  LIMIT -1 OFFSET ?"
            ")",
            (self.max_items,),
        )
        self._conn.commit()

    def all(self) -> list[Clip]:
        rows = self._conn.execute(
            "SELECT * FROM clips ORDER BY last_used_at DESC, id DESC"
        ).fetchall()
        return [self._row_to_clip(row) for row in rows]

    def get_data(self, clip_id: int) -> dict[str, bytes]:
        rows = self._conn.execute(
            "SELECT mime, data FROM clip_data WHERE clip_id = ?", (clip_id,)
        ).fetchall()
        return {row["mime"]: row["data"] for row in rows}

    def search(self, query: str) -> list[Clip]:
        """In-memory, casefold-based filter (SQLite LIKE breaks on Cyrillic)."""
        needle = normalize(query)
        if not needle:
            return self.all()
        return [clip for clip in self.all() if needle in normalize(clip.preview)]

    @staticmethod
    def _row_to_clip(row: sqlite3.Row) -> Clip:
        return Clip(
            id=row["id"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            kind=row["kind"],
            preview=row["preview"],
            hash=row["hash"],
            pinned=bool(row["pinned"]),
            use_count=row["use_count"],
            ocr_text=row["ocr_text"],
        )
