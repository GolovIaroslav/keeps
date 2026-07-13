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
from typing import TYPE_CHECKING

from keeps.text_encoding import decode_unicode_escapes, normalize_plain_text

if TYPE_CHECKING:
    from keeps.clip_archive import ArchiveClip
    from keeps.search import MatchReason, SearchIndex

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
# entries for v2 and beyond.
LATEST_VERSION = 5


def _migrate_v2_groups(conn: sqlite3.Connection) -> None:
    """Add flat groups plus the shared manual order used by scoped tabs."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clips)")}
    if "group_id" not in columns:
        conn.execute(
            "ALTER TABLE clips ADD COLUMN group_id INTEGER REFERENCES groups(id) "
            "ON DELETE SET NULL"
        )
    if "manual_order" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN manual_order REAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS groups ("
        "id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort_order INTEGER NOT NULL)"
    )


def _migrate_v3_alias(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clips)")}
    if "alias" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN alias TEXT")


def _migrate_v4_clip_hotkeys(conn: sqlite3.Connection) -> None:
    """Add the optional per-clip shortcut and its global/local scope."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(clips)")}
    if "hotkey" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN hotkey TEXT")
    if "hotkey_global" not in columns:
        conn.execute("ALTER TABLE clips ADD COLUMN hotkey_global INTEGER NOT NULL DEFAULT 0")


def _migrate_v5_copy_buffers(conn: sqlite3.Connection) -> None:
    """Add three persistent slots kept independently from clipboard history."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS copy_buffers ("
        "slot INTEGER PRIMARY KEY CHECK(slot BETWEEN 1 AND 3), "
        "kind TEXT NOT NULL, captured_at INTEGER NOT NULL, preview TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS copy_buffer_data ("
        "slot INTEGER NOT NULL REFERENCES copy_buffers(slot) ON DELETE CASCADE, "
        "mime TEXT NOT NULL, data BLOB NOT NULL, PRIMARY KEY(slot, mime))"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v2_groups,
    3: _migrate_v3_alias,
    4: _migrate_v4_clip_hotkeys,
    5: _migrate_v5_copy_buffers,
}

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
    group_id: int | None = None
    manual_order: float | None = None
    alias: str | None = None
    hotkey: str | None = None
    hotkey_global: bool = False


@dataclass(frozen=True)
class Group:
    id: int
    name: str
    sort_order: int


@dataclass(frozen=True)
class CopyBuffer:
    """One persistent, non-history clipboard buffer (PLAN.md Ф21)."""

    slot: int
    kind: str
    captured_at: int
    preview: str
    mime_data: dict[str, bytes]


def normalize(text: str) -> str:
    """Single source of truth for case-insensitive string comparison/search."""
    return text.casefold()


def normalize_with_mapping(text: str) -> tuple[str, list[int]]:
    """Casefold text and map each folded character back to its original index."""
    folded_parts = []
    original_indexes = []
    for index, character in enumerate(text):
        folded = normalize(character)
        folded_parts.append(folded)
        original_indexes.extend([index] * len(folded))
    return "".join(folded_parts), original_indexes


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
        text = normalize_plain_text(mime_data["text/plain"]).decode("utf-8")
        return text[:PREVIEW_MAX_CHARS]
    if kind == "html":
        text = normalize_plain_text(mime_data.get("text/plain", b"")).decode("utf-8")
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
        self._database_existed = (
            self._db_path.exists() and self._db_path.stat().st_size > 0
        )
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()
        from keeps.search import SearchIndex

        self._search_index: SearchIndex = SearchIndex()
        self._rebuild_search_index()

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            # SCHEMA brought both a brand-new DB and a pre-Ф10 legacy DB to
            # v1. From v2 onward real ALTERs follow, so an existing legacy
            # file must be protected even though it was never version-stamped.
            if self._database_existed and LATEST_VERSION > 1:
                backup_database(self._db_path, self._conn)
            self._conn.execute("BEGIN")
            try:
                for target in range(2, LATEST_VERSION + 1):
                    MIGRATIONS[target](self._conn)
                    self._conn.execute(f"PRAGMA user_version = {target}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            version = LATEST_VERSION
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

    def set_max_items(self, max_items: int) -> None:
        """Change the retention limit for the running daemon and trim now."""
        if max_items < 1:
            raise ValueError("max_items must be positive")
        self.max_items = max_items
        self.trim()

    def _rebuild_search_index(self) -> None:
        from keeps.search import CONTENT_LIMIT_BYTES

        rows = self._conn.execute(
            "SELECT c.id, c.kind, c.ocr_text, c.alias, d.mime, "
            "coalesce(substr(d.data, 1, ?), X'') AS data "
            "FROM clips c LEFT JOIN clip_data d ON d.clip_id = c.id AND ("
            "  (c.kind IN ('text', 'html') AND d.mime IN ('text/plain', 'text/html')) OR "
            "  (c.kind = 'files' AND d.mime = 'text/uri-list')"
            ") ORDER BY c.id, d.mime",
            (CONTENT_LIMIT_BYTES,),
        )
        current_id = None
        current_kind = ""
        current_ocr = None
        current_alias = None
        current_mime_data: dict[str, bytes] = {}
        for row in rows:
            if current_id is not None and row["id"] != current_id:
                self._search_index.upsert(
                    current_id, current_kind, current_mime_data, current_ocr, current_alias
                )
                current_mime_data = {}
            current_id = row["id"]
            current_kind = row["kind"]
            current_ocr = row["ocr_text"]
            current_alias = row["alias"]
            if row["mime"] is not None:
                current_mime_data[row["mime"]] = row["data"]
        if current_id is not None:
            self._search_index.upsert(
                current_id, current_kind, current_mime_data, current_ocr, current_alias
            )

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
            rows = self._conn.execute("DELETE FROM clips RETURNING id").fetchall()
        else:
            rows = self._conn.execute(
                "DELETE FROM clips WHERE pinned = 0 RETURNING id"
            ).fetchall()
        self._conn.commit()
        for row in rows:
            self._search_index.remove(row["id"])
        return len(rows)

    def add(self, kind: str, mime_data: dict[str, bytes]) -> int:
        """Insert a new clip, or move an existing duplicate to the top."""
        content_hash = hashlib.sha256(_canonical_bytes(kind, mime_data)).hexdigest()
        existing = self._conn.execute(
            "SELECT id FROM clips WHERE hash = ?", (content_hash,)
        ).fetchone()
        if existing is not None:
            clip_id = existing["id"]
            self.touch(clip_id)
            return clip_id
        return self._insert_new_clip(kind, mime_data, content_hash)

    def import_clip(self, clip: ArchiveClip) -> tuple[int, bool]:
        """Merge one portable archive clip without touching an existing duplicate.

        Return ``(clip_id, inserted)``. Unlike :meth:`add`, an already-known
        hash is intentionally left in place: importing a backup must not
        rewrite the user's history order or use count.
        """
        content_hash = hashlib.sha256(_canonical_bytes(clip.kind, clip.mime_data)).hexdigest()
        existing = self._conn.execute(
            "SELECT id FROM clips WHERE hash = ?", (content_hash,)
        ).fetchone()
        if existing is not None:
            return existing["id"], False
        clip_id = self._insert_new_clip(
            clip.kind,
            clip.mime_data,
            content_hash,
            pinned=clip.pinned,
            alias=clip.alias,
        )
        return clip_id, True

    def _insert_new_clip(
        self,
        kind: str,
        mime_data: dict[str, bytes],
        content_hash: str,
        *,
        pinned: bool = False,
        alias: str | None = None,
    ) -> int:
        now = int(time.time() * 1000)
        normalized_alias = alias.strip() or None if alias else None

        preview = build_preview(kind, mime_data)
        last_used_at = self._next_usage_timestamps(1)[0]
        cur = self._conn.execute(
            "INSERT INTO clips (created_at, last_used_at, kind, preview, hash, pinned, alias, "
            "use_count) VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (now, last_used_at, kind, preview, content_hash, int(pinned), normalized_alias),
        )
        clip_id = cur.lastrowid
        self._conn.executemany(
            "INSERT INTO clip_data (clip_id, mime, data) VALUES (?, ?, ?)",
            [(clip_id, mime, data) for mime, data in mime_data.items()],
        )
        self._conn.commit()
        self._search_index.upsert(clip_id, kind, mime_data, alias=normalized_alias)
        self.trim()
        return clip_id

    def touch(self, clip_id: int) -> None:
        """Move a clip to the top of the list (used-item invariant)."""
        last_used_at = self._next_usage_timestamps(1)[0]
        self._conn.execute(
            "UPDATE clips SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            (last_used_at, clip_id),
        )
        self._conn.commit()

    def touch_many(self, clip_ids: list[int]) -> None:
        """Move clips to the top while preserving the requested top-to-bottom order."""
        clip_ids = list(dict.fromkeys(clip_ids))
        timestamps = reversed(self._next_usage_timestamps(len(clip_ids)))
        self._conn.executemany(
            "UPDATE clips SET last_used_at = ?, use_count = use_count + 1 WHERE id = ?",
            list(zip(timestamps, clip_ids, strict=True)),
        )
        self._conn.commit()

    def _next_usage_timestamps(self, count: int) -> list[int]:
        """Reserve monotonic timestamps so every later use still becomes newest."""
        if count <= 0:
            return []
        row = self._conn.execute("SELECT max(last_used_at) AS latest FROM clips").fetchone()
        latest = row["latest"] if row is not None else None
        first = max(int(time.time() * 1000), (latest or 0) + 1)
        return list(range(first, first + count))

    def delete(self, clip_id: int) -> None:
        self._conn.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
        self._conn.commit()
        self._search_index.remove(clip_id)

    def delete_many(self, clip_ids: list[int]) -> int:
        clip_ids = list(dict.fromkeys(clip_ids))
        cur = self._conn.executemany(
            "DELETE FROM clips WHERE id = ?", [(clip_id,) for clip_id in clip_ids]
        )
        self._conn.commit()
        for clip_id in clip_ids:
            self._search_index.remove(clip_id)
        return cur.rowcount

    def update_content(self, clip_id: int, mime_data: dict[str, bytes]) -> int:
        """Replace a clip's content in place (used by external-editor Ctrl+E).

        Kind is preserved. If the edited content now matches another existing
        clip's hash, the two are merged (this clip is dropped, the other is
        touched) per the dedup invariant. Returns the resulting clip id.
        """
        row = self._conn.execute(
            "SELECT kind, ocr_text, alias FROM clips WHERE id = ?", (clip_id,)
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
        last_used_at = self._next_usage_timestamps(1)[0]
        self._conn.execute(
            "UPDATE clips SET preview = ?, hash = ?, last_used_at = ? WHERE id = ?",
            (preview, new_hash, last_used_at, clip_id),
        )
        self._conn.execute("DELETE FROM clip_data WHERE clip_id = ?", (clip_id,))
        self._conn.executemany(
            "INSERT INTO clip_data (clip_id, mime, data) VALUES (?, ?, ?)",
            [(clip_id, mime, data) for mime, data in mime_data.items()],
        )
        if kind == "image":
            self._conn.execute("DELETE FROM thumbs WHERE clip_id = ?", (clip_id,))
        self._conn.commit()
        self._search_index.upsert(
            clip_id, kind, mime_data, row["ocr_text"], row["alias"]
        )
        return clip_id

    def set_pinned(self, clip_id: int, pinned: bool) -> None:
        self._conn.execute(
            "UPDATE clips SET pinned = ? WHERE id = ?", (int(pinned), clip_id)
        )
        self._conn.commit()

    def set_pinned_many(self, clip_ids: list[int], pinned: bool) -> None:
        clip_ids = list(dict.fromkeys(clip_ids))
        self._conn.executemany(
            "UPDATE clips SET pinned = ? WHERE id = ?",
            [(int(pinned), clip_id) for clip_id in clip_ids],
        )
        self._conn.commit()

    def trim(self) -> None:
        """Delete the oldest unpinned clips beyond max_items."""
        rows = self._conn.execute(
            "DELETE FROM clips WHERE pinned = 0 AND id IN ("
            "  SELECT id FROM clips WHERE pinned = 0"
            "  ORDER BY last_used_at DESC, id DESC"
            "  LIMIT -1 OFFSET ?"
            ") RETURNING id",
            (self.max_items,),
        ).fetchall()
        self._conn.commit()
        for row in rows:
            self._search_index.remove(row["id"])

    def all(self) -> list[Clip]:
        rows = self._conn.execute(
            "SELECT * FROM clips ORDER BY last_used_at DESC, id DESC"
        ).fetchall()
        return [self._row_to_clip(row) for row in rows]

    def groups(self) -> list[Group]:
        rows = self._conn.execute(
            "SELECT id, name, sort_order FROM groups ORDER BY sort_order, id"
        ).fetchall()
        return [Group(row["id"], row["name"], row["sort_order"]) for row in rows]

    def clips_in_scope(self, scope: str, clips: list[Clip] | None = None) -> list[Clip]:
        clips = self.all() if clips is None else clips
        if scope == "history":
            return clips
        if scope == "pinned":
            scoped = [clip for clip in clips if clip.pinned]
        elif scope.startswith("group:"):
            group_id = int(scope.partition(":")[2])
            scoped = [clip for clip in clips if clip.group_id == group_id]
        else:
            raise ValueError(f"unknown scope: {scope}")
        history_position = {clip.id: position for position, clip in enumerate(clips)}
        return sorted(
            scoped,
            key=lambda clip: (
                clip.manual_order is None,
                clip.manual_order if clip.manual_order is not None else 0,
                history_position[clip.id],
            ),
        )

    def create_group(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("group name cannot be empty")
        next_order = self._conn.execute(
            "SELECT coalesce(max(sort_order), -1) + 1 FROM groups"
        ).fetchone()[0]
        cur = self._conn.execute(
            "INSERT INTO groups (name, sort_order) VALUES (?, ?)", (name, next_order)
        )
        self._conn.commit()
        return cur.lastrowid

    def rename_group(self, group_id: int, name: str) -> None:
        name = name.strip()
        if not name:
            raise ValueError("group name cannot be empty")
        self._conn.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))
        self._conn.commit()

    def delete_group(self, group_id: int) -> None:
        self._conn.execute("UPDATE clips SET group_id = NULL WHERE group_id = ?", (group_id,))
        self._conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        self._conn.commit()

    def set_group_many(self, clip_ids: list[int], group_id: int | None) -> None:
        self._conn.executemany(
            "UPDATE clips SET group_id = ?, manual_order = NULL WHERE id = ?",
            [(group_id, clip_id) for clip_id in dict.fromkeys(clip_ids)],
        )
        self._conn.commit()

    def move_manual(self, clip_id: int, scope: str, direction: int) -> None:
        if scope == "pinned":
            where, params = "pinned = 1", ()
        elif scope.startswith("group:"):
            where, params = "group_id = ?", (int(scope.partition(":")[2]),)
        else:
            raise ValueError("manual order is only available in pinned/group scopes")
        rows = self._conn.execute(
            f"SELECT id FROM clips WHERE {where} "
            "ORDER BY manual_order IS NULL, manual_order, last_used_at DESC, id DESC",
            params,
        ).fetchall()
        ids = [row["id"] for row in rows]
        index = ids.index(clip_id)
        target = max(0, min(len(ids) - 1, index + direction))
        if target == index:
            return
        ids[index], ids[target] = ids[target], ids[index]
        self._conn.executemany(
            "UPDATE clips SET manual_order = ? WHERE id = ?",
            [(position, item_id) for position, item_id in enumerate(ids)],
        )
        self._conn.commit()

    def get_data(self, clip_id: int) -> dict[str, bytes]:
        rows = self._conn.execute(
            "SELECT mime, data FROM clip_data WHERE clip_id = ?", (clip_id,)
        ).fetchall()
        return {
            row["mime"]: normalize_plain_text(row["data"])
            if row["mime"] == "text/plain"
            else row["data"]
            for row in rows
        }

    @staticmethod
    def _validate_copy_buffer(slot: int, kind: str, mime_data: dict[str, bytes]) -> None:
        if slot not in (1, 2, 3):
            raise ValueError("copy buffer slot must be 1, 2, or 3")
        # Reuse the history's supported canonical bundles. Arbitrary MIME
        # fidelity is deliberately deferred to Ф25.
        _canonical_bytes(kind, mime_data)

    def set_copy_buffer(self, slot: int, kind: str, mime_data: dict[str, bytes]) -> None:
        """Replace one persistent copy-buffer slot without touching history."""
        self._validate_copy_buffer(slot, kind, mime_data)
        captured_at = int(time.time() * 1000)
        preview = build_preview(kind, mime_data)
        self._conn.execute(
            "INSERT INTO copy_buffers(slot, kind, captured_at, preview) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(slot) DO UPDATE SET kind = excluded.kind, "
            "captured_at = excluded.captured_at, preview = excluded.preview",
            (slot, kind, captured_at, preview),
        )
        self._conn.execute("DELETE FROM copy_buffer_data WHERE slot = ?", (slot,))
        self._conn.executemany(
            "INSERT INTO copy_buffer_data(slot, mime, data) VALUES (?, ?, ?)",
            [(slot, mime, data) for mime, data in mime_data.items()],
        )
        self._conn.commit()

    def get_copy_buffer(self, slot: int) -> CopyBuffer | None:
        if slot not in (1, 2, 3):
            raise ValueError("copy buffer slot must be 1, 2, or 3")
        row = self._conn.execute(
            "SELECT slot, kind, captured_at, preview FROM copy_buffers WHERE slot = ?", (slot,)
        ).fetchone()
        if row is None:
            return None
        data_rows = self._conn.execute(
            "SELECT mime, data FROM copy_buffer_data WHERE slot = ? ORDER BY mime", (slot,)
        ).fetchall()
        return CopyBuffer(
            slot=row["slot"],
            kind=row["kind"],
            captured_at=row["captured_at"],
            preview=row["preview"],
            mime_data={data_row["mime"]: data_row["data"] for data_row in data_rows},
        )

    def copy_buffers(self) -> list[CopyBuffer]:
        return [
            buffer
            for slot in (1, 2, 3)
            if (buffer := self.get_copy_buffer(slot)) is not None
        ]

    def mime_sizes(self, clip_id: int) -> list[tuple[str, int]]:
        rows = self._conn.execute(
            "SELECT mime, length(data) AS size FROM clip_data WHERE clip_id = ? ORDER BY mime",
            (clip_id,),
        ).fetchall()
        return [(row["mime"], row["size"]) for row in rows]

    def set_alias(self, clip_id: int, alias: str | None) -> None:
        alias = alias.strip() if alias else None
        self._conn.execute("UPDATE clips SET alias = ? WHERE id = ?", (alias or None, clip_id))
        self._conn.commit()
        self._search_index.update_alias(clip_id, alias or "")

    def set_hotkey(self, clip_id: int, hotkey: str | None, *, global_hotkey: bool) -> None:
        """Persist one optional shortcut per clip.

        Conflict detection and actual KGlobalAccel registration live in the
        UI/runtime layer: SQLite only owns the durable assignment.
        """
        hotkey = hotkey.strip() if hotkey else None
        self._conn.execute(
            "UPDATE clips SET hotkey = ?, hotkey_global = ? WHERE id = ?",
            (hotkey, int(bool(hotkey) and global_hotkey), clip_id),
        )
        self._conn.commit()

    def hotkey_conflict(self, hotkey: str, *, exclude_clip_id: int | None = None) -> int | None:
        """Return another clip using `hotkey`, if one is assigned."""
        row = self._conn.execute(
            "SELECT id FROM clips WHERE hotkey = ? AND (? IS NULL OR id != ?)",
            (hotkey, exclude_clip_id, exclude_clip_id),
        ).fetchone()
        return row["id"] if row is not None else None

    def clips_with_hotkeys(self, *, global_only: bool = False) -> list[Clip]:
        """Assigned clips in the ordinary history order, optionally global only."""
        where = "hotkey IS NOT NULL"
        if global_only:
            where += " AND hotkey_global = 1"
        rows = self._conn.execute(
            f"SELECT * FROM clips WHERE {where} ORDER BY last_used_at DESC, id DESC"
        ).fetchall()
        return [self._row_to_clip(row) for row in rows]

    def get_thumbnail_source(self, clip_id: int) -> tuple[str, bytes] | None:
        """Return the current image content hash and full PNG for thumbnail work."""
        row = self._conn.execute(
            "SELECT c.hash, d.data FROM clips c "
            "JOIN clip_data d ON d.clip_id = c.id AND d.mime = 'image/png' "
            "WHERE c.id = ? AND c.kind = 'image'",
            (clip_id,),
        ).fetchone()
        return (row["hash"], row["data"]) if row is not None else None

    def set_thumbnail(self, clip_id: int, source_hash: str, png: bytes) -> bool:
        """Store a thumbnail only if the clip still has the source content hash."""
        cur = self._conn.execute(
            "INSERT INTO thumbs (clip_id, png) "
            "SELECT id, ? FROM clips WHERE id = ? AND kind = 'image' AND hash = ? "
            "ON CONFLICT(clip_id) DO UPDATE SET png = excluded.png",
            (png, clip_id, source_hash),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_thumbnail(self, clip_id: int) -> bytes | None:
        row = self._conn.execute(
            "SELECT png FROM thumbs WHERE clip_id = ?", (clip_id,)
        ).fetchone()
        return row["png"] if row is not None else None

    def search(self, query: str) -> list[Clip]:
        return self.search_with_reasons(query)[0]

    def search_with_reasons(
        self, query: str
    ) -> tuple[list[Clip], dict[int, MatchReason]]:
        """Full-content, casefolded AND search plus exact/OCR match reasons."""
        if not query.strip():
            return self.all(), {}
        reasons = self._search_index.search(query)
        return [clip for clip in self.all() if clip.id in reasons], reasons

    def search_snippet(
        self, clip_id: int, query: str, reason: MatchReason
    ) -> str | None:
        return self._search_index.snippet(clip_id, query, reason)

    def set_ocr_text(self, clip_id: int, text: str) -> None:
        self._conn.execute("UPDATE clips SET ocr_text = ? WHERE id = ?", (text, clip_id))
        self._conn.commit()
        self._search_index.update_ocr(clip_id, text)

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
        preview = row["preview"]
        if row["kind"] in ("text", "html"):
            preview = decode_unicode_escapes(preview)
        return Clip(
            id=row["id"],
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            kind=row["kind"],
            preview=preview,
            hash=row["hash"],
            pinned=bool(row["pinned"]),
            use_count=row["use_count"],
            ocr_text=row["ocr_text"],
            group_id=row["group_id"],
            manual_order=row["manual_order"],
            alias=row["alias"],
            hotkey=row["hotkey"],
            hotkey_global=bool(row["hotkey_global"]),
        )
