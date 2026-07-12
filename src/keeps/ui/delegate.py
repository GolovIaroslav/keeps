"""List item rendering: preview wrapping, thumbnails, kind badge, relative time."""

from __future__ import annotations

import time

from PySide6.QtCore import QModelIndex, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QFont,
    QFontMetrics,
    QPen,
    QPixmap,
    QPixmapCache,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from keeps.store import Store
from keeps.ui.format import highlight_ranges, relative_time

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
        self._thumbnail_cache_keys: dict[int, str] = {}

    def set_scale(self, scale: float) -> None:
        """Scale thumbnail/padding pixel sizes to match the UI scale (Ctrl+scroll)."""
        self._scale = scale

    def _thumbnail_size(self) -> int:
        return round(THUMBNAIL_SIZE * self._scale)

    def _padding(self) -> int:
        return round(PADDING * self._scale)

    def invalidate_thumbnail(self, clip_id: int) -> None:
        cache_key = self._thumbnail_cache_keys.pop(clip_id, None)
        if cache_key is not None:
            QPixmapCache.remove(cache_key)

    def prune_thumbnail_cache(self, existing_clip_ids: set[int]) -> None:
        for clip_id in self._thumbnail_cache_keys.keys() - existing_clip_ids:
            self.invalidate_thumbnail(clip_id)

    def _thumbnail_pixmap(self, clip_id: int, clip_hash: str) -> QPixmap | None:
        cache_key = f"keeps-thumbnail:{clip_id}:{clip_hash}"
        previous_key = self._thumbnail_cache_keys.get(clip_id)
        if previous_key is not None and previous_key != cache_key:
            QPixmapCache.remove(previous_key)
            self._thumbnail_cache_keys.pop(clip_id, None)

        pixmap = QPixmapCache.find(cache_key)
        if pixmap is not None:
            return pixmap

        png = self._store.get_thumbnail(clip_id)
        pixmap = QPixmap()
        if png is None or not pixmap.loadFromData(png, "PNG"):
            return None
        QPixmapCache.insert(cache_key, pixmap)
        self._thumbnail_cache_keys[clip_id] = cache_key
        return pixmap

    @staticmethod
    def _draw_highlighted_preview(painter, option, rect: QRect, text: str, query: str) -> None:
        ranges = highlight_ranges(text, query)
        if not ranges:
            painter.drawText(
                rect,
                int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
                text,
            )
            return

        highlight_format = QTextCharFormat()
        if option.state & QStyle.StateFlag.State_Selected:
            highlight_format.setBackground(option.palette.base())
            highlight_format.setForeground(option.palette.text())
        else:
            highlight_format.setBackground(option.palette.highlight())
            highlight_format.setForeground(option.palette.highlightedText())
        highlight_format.setFontWeight(QFont.Weight.Bold)

        formats = []
        for start, length in ranges:
            format_range = QTextLayout.FormatRange()
            format_range.start = start
            format_range.length = length
            format_range.format = highlight_format
            formats.append(format_range)

        layout = QTextLayout(text, option.font)
        text_option = QTextOption()
        text_option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(text_option)
        layout.setFormats(formats)
        layout.beginLayout()
        y = 0.0
        for _ in range(MAX_PREVIEW_LINES):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            line.setPosition(QPointF(0, y))
            y += line.height()
        layout.endLayout()
        painter.save()
        painter.setClipRect(rect)
        layout.draw(painter, QPointF(rect.left(), rect.top()))
        painter.restore()

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
            pixmap = self._thumbnail_pixmap(clip.id, clip.hash)
            if pixmap is not None:
                scaled_pixmap = pixmap.scaled(
                    thumbnail_size,
                    thumbnail_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                painter.drawPixmap(rect.left(), rect.top(), scaled_pixmap)
            if clip.ocr_text and clip.ocr_text.strip():
                original_font = painter.font()
                badge_font = painter.font()
                badge_font.setPointSize(max(1, option.font.pointSize() - 2))
                badge_metrics = QFontMetrics(badge_font)
                badge_width = badge_metrics.horizontalAdvance("OCR") + 4
                badge_height = badge_metrics.height() + 2
                badge_rect = QRect(
                    rect.left() + thumbnail_size - badge_width,
                    rect.top() + thumbnail_size - badge_height,
                    badge_width,
                    badge_height,
                )
                painter.setFont(badge_font)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(option.palette.highlight())
                painter.drawRect(badge_rect)
                painter.setPen(option.palette.highlightedText().color())
                painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, "OCR")
                painter.setFont(original_font)
                painter.setPen(text_color)
            rect.setLeft(rect.left() + thumbnail_size + padding)

        if clip.pinned:
            painter.setBrush(text_color)
            painter.drawEllipse(QRect(rect.right() - 10, rect.top(), 8, 8))
            rect.setRight(rect.right() - 14)

        age = relative_time(clip.last_used_at, int(time.time() * 1000))
        meta_parts = [KIND_LABELS.get(clip.kind, clip.kind.upper())]
        match_reason = index.model().match_reason(clip.id)
        if match_reason:
            meta_parts.append(f"[{match_reason.value}]")
        meta_parts.append(age)
        meta = " · ".join(meta_parts)
        painter.drawText(
            QRect(rect.left(), rect.top(), rect.width(), line_height),
            Qt.AlignmentFlag.AlignRight,
            meta,
        )

        preview_rect = QRect(
            rect.left(), rect.top() + line_height, rect.width(), line_height * MAX_PREVIEW_LINES
        )
        display_text = index.model().display_text(clip)
        self._draw_highlighted_preview(
            painter,
            option,
            preview_rect,
            display_text,
            index.model().current_query,
        )
        painter.restore()
