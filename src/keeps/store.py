"""SQLite-backed clip history: schema, dedup/move-to-top, trim, search."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import struct
import time
from collections.abc import Callable
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
CREATE TABLE IF NOT EXISTS embeddings (
  clip_id INTEGER PRIMARY KEY REFERENCES clips(id) ON DELETE CASCADE,
  model   TEXT NOT NULL,
  vec     BLOB NOT NULL
);
"""

PREVIEW_MAX_CHARS = 300

# Schema version history (PLAN.md §5/Ф10). SCHEMA above (CREATE TABLE IF NOT
# EXISTS) always brings any DB -- brand new, or one that predates this
# migration system entirely -- up to the v1 baseline; MIGRATIONS only needs
# entries for v2 and beyond. First real migration lands in Ф14 (groups).
LATEST_VERSION = 1
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {}

BACKUP_KEEP = 3


def backup_database(db_path: Path, conn: sqlite3.Connection | None = None) -> Path:
    """Copy db_path to a timestamped sibling, then rotate old backups.

    Checkpoints WAL first (if a connection is given) so the copy is a
    self-consistent snapshot restorable by plain file copy.
    """
    db_path = Path(db_path)
    if conn is not None:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup_path = db_path.with_name(f"{db_path.name}.backup-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(db_path, backup_path)
    _rotate_backups(db_path)
    return backup_path


def _rotate_backups(db_path: Path, keep: int = BACKUP_KEEP) -> None:
    """Keep only the `keep` newest backups (filenames sort chronologically)."""
    backups = sorted(db_path.parent.glob(f"{db_path.name}.backup-*"))
    for stale in backups[:-keep]:
        stale.unlink()


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
        self._db_path = Path(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # SCHEMA already brought this DB up to the v1 baseline above --
            # whether it's brand new or predates this migration system
            # entirely -- so no real migration ran and no backup is needed
            # just to stamp the version.
            self._conn.execute("PRAGMA user_version = 1")
            self._conn.commit()
            version = 1
        if version >= LATEST_VERSION:
            return
        backup_database(self._db_path, self._conn)
        self._conn.execute("BEGIN")
        try:
            for target in range(version + 1, LATEST_VERSION + 1):
                MIGRATIONS[target](self._conn)
                self._conn.execute(f"PRAGMA user_version = {target}")
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def backup_now(self) -> Path:
        """Manual backup, e.g. Settings > Database > Backup now."""
        return backup_database(self._db_path, self._conn)

    def compact(self) -> tuple[int, int]:
        """VACUUM the DB file; returns (size_before, size_after) in bytes.

        Checkpoints WAL first so "before" reflects committed data (including
        free pages from prior deletes) rather than an arbitrary partially
        checkpointed main file -- WAL auto-checkpoints only every ~1000
        pages, so without this a bulk delete right before Compact could
        still show as "no change" simply because it hadn't been
        checkpointed into the main file yet.
        """
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = self._db_path.stat().st_size
        self._conn.execute("VACUUM")
        # In WAL mode VACUUM's own writes land in a fresh WAL; without this
        # checkpoint the file on disk still shows the pre-VACUUM size until
        # the connection is closed.
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = self._db_path.stat().st_size
        return before, after

    def clear_history(self, include_pinned: bool = False) -> int:
        """Delete clips (optionally including pinned). Returns count deleted."""
        if include_pinned:
            cur = self._conn.execute("DELETE FROM clips")
        else:
            cur = self._conn.execute("DELETE FROM clips WHERE pinned = 0")
        self._conn.commit()
        return cur.rowcount

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
        if kind == "image":
            self._conn.execute("DELETE FROM thumbs WHERE clip_id = ?", (clip_id,))
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

    def set_thumbnail(self, clip_id: int, png: bytes) -> bool:
        """Store an image thumbnail; return False if its clip no longer exists."""
        cur = self._conn.execute(
            "INSERT INTO thumbs (clip_id, png) "
            "SELECT id, ? FROM clips WHERE id = ? AND kind = 'image' "
            "ON CONFLICT(clip_id) DO UPDATE SET png = excluded.png",
            (png, clip_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_thumbnail(self, clip_id: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT png FROM thumbs WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        return row["png"] if row is not None else None

    def search(self, query: str) -> list[Clip]:
        """In-memory, casefold-based filter (SQLite LIKE breaks on Cyrillic).

        Matches preview and, when present, ocr_text -- OCR-recognized text
        always participates in plain substring search, independent of any
        ai/* toggle (PLAN.md §9).
        """
        needle = normalize(query)
        if not needle:
            return self.all()
        return [clip for clip in self.all() if self._matches(clip, needle)]

    @staticmethod
    def _matches(clip: Clip, needle: str) -> bool:
        if needle in normalize(clip.preview):
            return True
        return clip.ocr_text is not None and needle in normalize(clip.ocr_text)

    def set_ocr_text(self, clip_id: int, text: str) -> None:
        self._conn.execute("UPDATE clips SET ocr_text = ? WHERE id = ?", (text, clip_id))
        self._conn.commit()

    def set_embedding(self, clip_id: int, model: str, vec: bytes) -> None:
        self._conn.execute(
            "INSERT INTO embeddings (clip_id, model, vec) VALUES (?, ?, ?) "
            "ON CONFLICT(clip_id) DO UPDATE SET model = excluded.model, vec = excluded.vec",
            (clip_id, model, vec),
        )
        self._conn.commit()

    def get_all_embeddings(self, model: str) -> list[tuple[int, bytes]]:
        rows = self._conn.execute(
            "SELECT clip_id, vec FROM embeddings WHERE model = ?", (model,)
        ).fetchall()
        return [(row["clip_id"], row["vec"]) for row in rows]

    def clips_missing_ocr(self) -> list[int]:
        """Image-clip ids with no ocr_text yet -- used for the OCR backlog sweep."""
        rows = self._conn.execute(
            "SELECT id FROM clips WHERE kind = 'image' AND ocr_text IS NULL"
        ).fetchall()
        return [row["id"] for row in rows]

    def clips_missing_thumbnail(self) -> list[int]:
        """Image-clip ids without a generated thumbnail, for the startup sweep."""
        rows = self._conn.execute(
            "SELECT c.id FROM clips c "
            "LEFT JOIN thumbs t ON t.clip_id = c.id "
            "WHERE c.kind = 'image' AND t.clip_id IS NULL "
            "ORDER BY c.id"
        ).fetchall()
        return [row["id"] for row in rows]

    def clips_missing_embedding(self, model: str) -> list[int]:
        """Text/html clip ids with no `model` embedding yet -- text-RAG backlog sweep."""
        rows = self._conn.execute(
            "SELECT c.id FROM clips c "
            "LEFT JOIN embeddings e ON e.clip_id = c.id AND e.model = ? "
            "WHERE c.kind IN ('text', 'html') AND e.clip_id IS NULL",
            (model,),
        ).fetchall()
        return [row["id"] for row in rows]

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
