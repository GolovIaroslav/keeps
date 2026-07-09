"""List item rendering: preview wrapping, thumbnails, kind badge, relative time."""

from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QImage, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from keeps.store import Store

KIND_LABELS = {"text": "TXT", "html": "HTML", "image": "IMG", "files": "FILES"}

THUMBNAIL_SIZE = 40
MAX_PREVIEW_LINES = 3
PADDING = 6
NUMBER_BADGE_COUNT = 9  # matches Ctrl+1..9


def relative_time(timestamp_ms: int, now_ms: int) -> str:
    """Human-readable age, e.g. 'just now', '5m ago', '2h ago', or a date."""
    delta_s = max(0, (now_ms - timestamp_ms) // 1000)
    if delta_s < 5:
        return "just now"
    if delta_s < 60:
        return f"{delta_s}s ago"
    if delta_s < 3600:
        return f"{delta_s // 60}m ago"
    if delta_s < 86400:
        return f"{delta_s // 3600}h ago"
    if delta_s < 7 * 86400:
        return f"{delta_s // 86400}d ago"
    return datetime.fromtimestamp(timestamp_ms / 1000).strftime("%Y-%m-%d")


class ClipItemDelegate(QStyledItemDelegate):
    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self._store = store

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        metrics = QFontMetrics(option.font)
        line_height = metrics.lineSpacing()
        text_block = line_height * (1 + MAX_PREVIEW_LINES)
        height = max(THUMBNAIL_SIZE, text_block) + 2 * PADDING
        return QSize(option.rect.width(), height)

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        clip = index.model().clip_at(index.row())

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            text_color = option.palette.highlightedText().color()
        else:
            text_color = option.palette.text().color()
        painter.setPen(text_color)

        rect = option.rect.adjusted(PADDING, PADDING, -PADDING, -PADDING)
        metrics = QFontMetrics(option.font)
        line_height = metrics.lineSpacing()

        if index.row() < NUMBER_BADGE_COUNT:
            painter.drawText(
                QRect(rect.left(), rect.top(), 16, line_height),
                Qt.AlignmentFlag.AlignLeft,
                str(index.row() + 1),
            )
            rect.setLeft(rect.left() + 18)

        if clip.kind == "image":
            png = self._store.get_data(clip.id).get("image/png")
            image = QImage.fromData(png, "PNG") if png else QImage()
            if not image.isNull():
                pixmap = QPixmap.fromImage(image).scaled(
                    THUMBNAIL_SIZE,
                    THUMBNAIL_SIZE,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(rect.left(), rect.top(), pixmap)
            rect.setLeft(rect.left() + THUMBNAIL_SIZE + PADDING)

        if clip.pinned:
            painter.setBrush(text_color)
            painter.drawEllipse(QRect(rect.right() - 10, rect.top(), 8, 8))
            rect.setRight(rect.right() - 14)

        age = relative_time(clip.last_used_at, int(time.time() * 1000))
        meta = f"{KIND_LABELS.get(clip.kind, clip.kind.upper())} · {age}"
        painter.drawText(
            QRect(rect.left(), rect.top(), rect.width(), line_height),
            Qt.AlignmentFlag.AlignRight,
            meta,
        )

        preview_rect = QRect(
            rect.left(), rect.top() + line_height, rect.width(), line_height * MAX_PREVIEW_LINES
        )
        painter.drawText(
            preview_rect,
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
            clip.preview,
        )
        painter.restore()
