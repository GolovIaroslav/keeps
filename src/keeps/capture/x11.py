"""Clipboard capture on X11 (and as an Xwayland fallback) via QClipboard."""

from __future__ import annotations

from PySide6.QtCore import QBuffer, QIODevice, QObject
from PySide6.QtGui import QClipboard, QGuiApplication, QImage

from keeps.capture.base import (
    DEFAULT_MAX_ITEM_MB,
    MIME_HTML,
    MIME_IMAGE,
    MIME_PLAIN,
    MIME_URI_LIST,
    SelfSetGuard,
    build_bundle,
)
from keeps.store import Store


class X11Watcher(QObject):
    def __init__(
        self,
        store: Store,
        max_item_mb: float = DEFAULT_MAX_ITEM_MB,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._max_item_mb = max_item_mb
        self.guard = SelfSetGuard()
        self._clipboard: QClipboard = QGuiApplication.clipboard()
        self._mime_data = None
        self._clipboard.dataChanged.connect(self._on_changed)

    def start(self) -> None:
        pass  # QClipboard is already live once QApplication exists.

    def stop(self) -> None:
        self._clipboard.dataChanged.disconnect(self._on_changed)

    def _on_changed(self) -> None:
        if self.guard.consume_skip():
            return
        self._mime_data = self._clipboard.mimeData()
        available = self._available_types()
        result = build_bundle(available, self._read_mime, self._max_item_mb)
        self._mime_data = None
        if result is None:
            return
        kind, mime_data = result
        self._store.add(kind, mime_data)

    def _available_types(self) -> set[str]:
        available = set()
        if self._mime_data.hasText():
            available.add(MIME_PLAIN)
        if self._mime_data.hasHtml():
            available.add(MIME_HTML)
        if self._mime_data.hasImage():
            available.add(MIME_IMAGE)
        if self._mime_data.hasUrls():
            available.add(MIME_URI_LIST)
        return available

    def _read_mime(self, mime: str) -> bytes:
        if mime == MIME_PLAIN:
            return self._mime_data.text().encode("utf-8")
        if mime == MIME_HTML:
            return self._mime_data.html().encode("utf-8")
        if mime == MIME_URI_LIST:
            urls = [url.toString() for url in self._mime_data.urls()]
            return "\n".join(urls).encode("utf-8")
        if mime == MIME_IMAGE:
            image: QImage = self._mime_data.imageData()
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            return bytes(buffer.data())
        raise ValueError(f"unknown mime: {mime}")
