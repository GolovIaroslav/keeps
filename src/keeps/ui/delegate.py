"""List item rendering: preview wrapping, thumbnails, kind badge, relative time."""

from __future__ import annotations

import time

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QFontMetrics, QImage, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from keeps.store import Store
from keeps.ui.format import relative_time

KIND_LABELS = {"text": "TXT", "html": "HTML", "image": "IMG", "files": "FILES"}

THUMBNAIL_SIZE = 40
MAX_PREVIEW_LINES = 3
PADDING = 6
NUMBER_BADGE_COUNT = 9  # matches Ctrl+1..9
PASTED_BORDER_WIDTH = 2  # Ф9.3: border around every clip pasted this popup session


class ClipItemDelegate(QStyledItemDelegate):
    def __init__(self, store: Store, parent=None) -> None:
        super().__init__(parent)
        self._store = store
        self._scale = 1.0

    def set_scale(self, scale: float) -> None:
        """Scale thumbnail/padding pixel sizes to match the UI scale (Ctrl+scroll)."""
        self._scale = scale

    def _thumbnail_size(self) -> int:
        return round(THUMBNAIL_SIZE * self._scale)

    def _padding(self) -> int:
        return round(PADDING * self._scale)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        metrics = QFontMetrics(option.font)
        line_height = metrics.lineSpacing()
        text_block = line_height * (1 + MAX_PREVIEW_LINES)
        height = max(self._thumbnail_size(), text_block) + 2 * self._padding()
        return QSize(option.rect.width(), height)

    def paint(self, painter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        clip = index.model().clip_at(index.row())

        painter.save()
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            text_color = option.palette.highlightedText().color()
        else:
            if index.row() % 2:
                painter.fillRect(option.rect, option.palette.alternateBase())
            text_color = option.palette.text().color()
        painter.setPen(QPen(option.palette.mid().color()))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.setPen(text_color)

        if clip.id in index.model().pasted_ids:
            border_pen = QPen(option.palette.highlight().color())
            border_pen.setWidth(PASTED_BORDER_WIDTH)
            painter.setPen(border_pen)
            painter.drawRect(option.rect.adjusted(1, 1, -2, -2))
            painter.setPen(text_color)

        padding = self._padding()
        rect = option.rect.adjusted(padding, padding, -padding, -padding)
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
            thumbnail_size = self._thumbnail_size()
            png = self._store.get_data(clip.id).get("image/png")
            image = QImage.fromData(png, "PNG") if png else QImage()
            if not image.isNull():
                pixmap = QPixmap.fromImage(image).scaled(
                    thumbnail_size,
                    thumbnail_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(rect.left(), rect.top(), pixmap)
            rect.setLeft(rect.left() + thumbnail_size + padding)

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
