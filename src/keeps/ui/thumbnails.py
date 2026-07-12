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
    finished = Signal(int, object)  # (clip_id, png_bytes | None)


class _ThumbnailTask(QRunnable):
    def __init__(self, clip_id: int, png_bytes: bytes, signals: _ThumbnailSignals) -> None:
        super().__init__()
        self._clip_id = clip_id
        self._png_bytes = png_bytes
        self._signals = signals

    def run(self) -> None:
        self._signals.finished.emit(self._clip_id, generate_thumbnail(self._png_bytes))


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
            png_bytes = self._store.get_data(clip_id).get("image/png")
            if png_bytes is None:
                self._queued_ids.discard(clip_id)
                continue
            self._active_clip_id = clip_id
            signals = _ThumbnailSignals(self)
            signals.finished.connect(self._on_thumbnail_done)
            self._pool.start(
                _ThumbnailTask(clip_id, png_bytes, signals),
                THUMBNAIL_TASK_PRIORITY,
            )
            return

    def _on_thumbnail_done(self, clip_id: int, png_bytes: bytes | None) -> None:
        self._active_clip_id = None
        self._queued_ids.discard(clip_id)
        if png_bytes is not None and self._store.set_thumbnail(clip_id, png_bytes):
            self.thumbnail_ready.emit(clip_id)
        self._start_next()
