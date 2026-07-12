"""Asynchronous, one-time image thumbnail generation (PLAN.md Ф11)."""

from __future__ import annotations

from collections import deque

from PySide6.QtCore import QBuffer, QIODevice, QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage

from keeps.store import Store

MAX_THUMBNAIL_EDGE = 256
THUMBNAIL_TASK_PRIORITY = -1


def generate_thumbnail(png_bytes: bytes) -> bytes | None:
    """Decode and re-encode a PNG scaled to fit within 256x256 pixels."""
    image = QImage.fromData(png_bytes, "PNG")
    if image.isNull():
        return None
    if image.width() > MAX_THUMBNAIL_EDGE or image.height() > MAX_THUMBNAIL_EDGE:
        image = image.scaled(
            MAX_THUMBNAIL_EDGE,
            MAX_THUMBNAIL_EDGE,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        return None
    return bytes(buffer.data())


class _ThumbnailSignals(QObject):
    finished = Signal(int, str, object)  # (clip_id, source_hash, png_bytes | None)


class _ThumbnailTask(QRunnable):
    def __init__(
        self,
        clip_id: int,
        source_hash: str,
        png_bytes: bytes,
        signals: _ThumbnailSignals,
    ) -> None:
        super().__init__()
        self._clip_id = clip_id
        self._source_hash = source_hash
        self._png_bytes = png_bytes
        self._signals = signals

    def run(self) -> None:
        self._signals.finished.emit(
            self._clip_id,
            self._source_hash,
            generate_thumbnail(self._png_bytes),
        )


class ThumbnailRuntime(QObject):
    """Schedules thumbnail work while keeping all SQLite access on the main thread."""

    thumbnail_ready = Signal(int)  # clip_id

    def __init__(self, store: Store, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._queued_ids: set[int] = set()
        self._queue: deque[int] = deque()
        self._active_clip_id: int | None = None
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._signals = _ThumbnailSignals(self)
        self._signals.finished.connect(self._on_thumbnail_done)

    def on_clip_captured(self, clip_id: int, kind: str) -> None:
        if kind == "image":
            self._enqueue(clip_id)

    def run_backlog_sweep(self) -> None:
        for clip_id in self._store.clips_missing_thumbnail():
            self._enqueue(clip_id)

    def _enqueue(self, clip_id: int) -> None:
        if clip_id in self._queued_ids or self._store.get_thumbnail(clip_id) is not None:
            return
        self._queued_ids.add(clip_id)
        self._queue.append(clip_id)
        self._start_next()

    def _start_next(self) -> None:
        if self._active_clip_id is not None:
            return
        while self._queue:
            clip_id = self._queue.popleft()
            source = self._store.get_thumbnail_source(clip_id)
            if source is None:
                self._queued_ids.discard(clip_id)
                continue
            source_hash, png_bytes = source
            self._active_clip_id = clip_id
            self._pool.start(
                _ThumbnailTask(clip_id, source_hash, png_bytes, self._signals),
                THUMBNAIL_TASK_PRIORITY,
            )
            return

    def _on_thumbnail_done(
        self, clip_id: int, source_hash: str, png_bytes: bytes | None
    ) -> None:
        self._active_clip_id = None
        self._queued_ids.discard(clip_id)
        if png_bytes is not None:
            if self._store.set_thumbnail(clip_id, source_hash, png_bytes):
                self.thumbnail_ready.emit(clip_id)
            else:
                # The clip was edited or its SQLite id was reused while the
                # worker was decoding. Queue the current content, if any.
                self._enqueue(clip_id)
        self._start_next()
