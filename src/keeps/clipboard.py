"""Value-copy helpers for short-lived clipboard transactions."""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImage


def make_mime_data(mime_data: dict[str, bytes], *, plain_only: bool = False) -> QMimeData:
    """Create a Qt clipboard payload from Keeps' supported canonical formats."""
    result = QMimeData()
    plain = mime_data.get("text/plain")
    if plain is not None:
        result.setText(plain.decode("utf-8", errors="replace"))
    if plain_only:
        return result
    html = mime_data.get("text/html")
    if html is not None:
        result.setHtml(html.decode("utf-8", errors="replace"))
    png = mime_data.get("image/png")
    if png is not None:
        result.setImageData(QImage.fromData(png, "PNG"))
    uri_list = mime_data.get("text/uri-list")
    if uri_list is not None:
        result.setUrls([QUrl(line) for line in uri_list.decode("utf-8").splitlines() if line])
    return result


def snapshot_mime_data(source: QMimeData, *, max_bytes: int) -> dict[str, bytes] | None:
    """Detach all Qt-exposed clipboard formats, bounded to protect the UI."""
    snapshot: dict[str, bytes] = {}
    total = 0
    for mime in source.formats():
        data = bytes(source.data(mime))
        total += len(data)
        if total > max_bytes:
            return None
        snapshot[mime] = data
    return snapshot


def restore_mime_data(snapshot: dict[str, bytes]) -> QMimeData:
    """Make a fresh raw multi-MIME value copy for restoring a clipboard snapshot."""
    result = QMimeData()
    for mime, data in snapshot.items():
        result.setData(mime, data)
    return result
