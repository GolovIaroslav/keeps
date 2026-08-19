"""Ф9.2: read-only "View" expand + built-in "Edit" dialogs for a selected clip.

Separate from the existing external-editor flow (ui/popup.py::_edit_current,
Ctrl+E, xdg-open+temp-file+QFileSystemWatcher) which is unrelated and unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QKeySequence,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from keeps.store import Clip, normalize
from keeps.ui.format import format_byte_size, text_statistics

_DEFAULT_SIZE = QSize(480, 400)
_FIND_MATCH_LIMIT = 10_000


def _codepoint_offset(text: str, normalized_offset: int) -> int:
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        if len(normalize(text[: middle + 1])) > normalized_offset:
            high = middle
        else:
            low = middle + 1
    return low


def _match_spans(text: str, query: str) -> list[tuple[int, int]]:
    """Return non-overlapping matches using the popup's Unicode normalization."""
    normalized_text = normalize(text)
    normalized_query = normalize(query)
    if not normalized_query:
        return []
    spans = []
    offset = 0
    identity_mapping = len(normalized_text) == len(text)
    while (
        len(spans) < _FIND_MATCH_LIMIT
        and (position := normalized_text.find(normalized_query, offset)) >= 0
    ):
        end_position = position + len(normalized_query) - 1
        span = (
            (position, end_position + 1)
            if identity_mapping
            else (
                _codepoint_offset(text, position),
                _codepoint_offset(text, end_position) + 1,
            )
        )
        if not spans or spans[-1] != span:
            spans.append(span)
        offset = position + len(normalized_query)
    return spans


def _utf16_offset(text: str, codepoint_offset: int) -> int:
    return len(text[:codepoint_offset].encode("utf-16-le")) // 2


class _FindBar(QWidget):
    """Small Ctrl+F bar backed by QTextDocument's case-insensitive search."""

    def __init__(self, editor: QPlainTextEdit, parent=None) -> None:
        super().__init__(parent)
        self._editor = editor
        self._matches: list[tuple[int, int]] = []
        self._current_match = -1
        self._matches_truncated = False
        self._query = QLineEdit(self)
        self._query.setPlaceholderText(self.tr("Find in clip..."))
        self._counter = QLabel("0 / 0", self)
        previous = QToolButton(self)
        previous.setText("↑")
        previous.setToolTip(self.tr("Previous match (Shift+Enter)"))
        previous.clicked.connect(self.find_previous)
        following = QToolButton(self)
        following.setText("↓")
        following.setToolTip(self.tr("Next match (Enter)"))
        following.clicked.connect(self.find_next)
        close_button = QToolButton(self)
        close_button.setText("×")
        close_button.setToolTip(self.tr("Close search"))
        close_button.clicked.connect(self.close)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._query, 1)
        layout.addWidget(self._counter)
        layout.addWidget(previous)
        layout.addWidget(following)
        layout.addWidget(close_button)

        self._query.textChanged.connect(self._find_first)
        self._editor.textChanged.connect(self._find_first)
        self._query.returnPressed.connect(self.find_next)
        QShortcut(QKeySequence("Shift+Return"), self._query).activated.connect(
            self.find_previous
        )
        QShortcut(QKeySequence("Shift+Enter"), self._query).activated.connect(
            self.find_previous
        )

    def open(self, query: str | None = None) -> None:
        self.show()
        if query is not None and query != self._query.text():
            self._query.setText(query)
        elif self._query.text():
            self._find_first()
        self._query.setFocus()
        self._query.selectAll()

    def _find_first(self) -> None:
        self._matches = _match_spans(self._editor.toPlainText(), self._query.text())
        total = normalize(self._editor.toPlainText()).count(normalize(self._query.text()))
        self._matches_truncated = total > len(self._matches)
        self._current_match = 0 if self._matches else -1
        self._select_current_match()

    def find_next(self) -> None:
        if not self._matches:
            self._find_first()
            return
        self._current_match = (self._current_match + 1) % len(self._matches)
        self._select_current_match()

    def find_previous(self) -> None:
        if not self._matches:
            self._find_first()
            return
        self._current_match = (self._current_match - 1) % len(self._matches)
        self._select_current_match()

    def _select_current_match(self) -> None:
        if self._current_match < 0:
            self._editor.setExtraSelections([])
            self._counter.setText("0 / 0")
            return
        start, end = self._matches[self._current_match]
        text = self._editor.toPlainText()
        match = QTextCursor(self._editor.document())
        match.setPosition(_utf16_offset(text, start))
        match.setPosition(
            _utf16_offset(text, end), QTextCursor.MoveMode.KeepAnchor
        )
        self._editor.setTextCursor(match)
        self._editor.ensureCursorVisible()
        highlight = QTextEdit.ExtraSelection()
        highlight.cursor = match
        highlight.format.setBackground(QColor("#f6c343"))
        highlight.format.setForeground(QColor("#171717"))
        self._editor.setExtraSelections([highlight])

        suffix = "+" if self._matches_truncated else ""
        self._counter.setText(
            f"{self._current_match + 1} / {len(self._matches)}{suffix}"
        )


def _clip_text(clip: Clip, mime_data: dict[str, bytes]) -> str:
    """Full text for a clip, per-kind, mirroring store.py's build_preview() dispatch."""
    if clip.kind == "files":
        raw = mime_data.get("text/uri-list", b"")
    else:
        raw = mime_data.get("text/plain") or mime_data.get("text/html", b"")
    return raw.decode("utf-8", errors="replace")


class ViewDialog(QDialog):
    """Read-only expand: full wrapped text, or the image at (up to) full size."""

    def __init__(
        self,
        clip: Clip,
        mime_data: dict[str, bytes],
        parent=None,
        *,
        initial_query: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("View"))
        layout = QVBoxLayout(self)

        details = QLabel(self._details_text(clip, mime_data))
        details.setWordWrap(True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(details)

        self._text_view: QPlainTextEdit | None = None
        if clip.kind == "image":
            layout.addWidget(self._build_image_label(mime_data))
            if clip.ocr_text and clip.ocr_text.strip():
                self._text_view = QPlainTextEdit()
                self._text_view.setPlainText(clip.ocr_text)
                self._text_view.setReadOnly(True)
                self._text_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        else:
            self._text_view = self._build_text_view(clip, mime_data)

        self._find_bar: _FindBar | None = None
        if self._text_view is not None:
            self._find_bar = _FindBar(self._text_view, self)
            self._find_bar.hide()
            layout.addWidget(self._find_bar)
            layout.addWidget(self._text_view)
            QShortcut(QKeySequence.StandardKey.Find, self).activated.connect(self.show_find)
            if initial_query.strip():
                self._find_bar.open(initial_query)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.resize(_DEFAULT_SIZE)

    def show_find(self) -> None:
        if self._find_bar is not None:
            self._find_bar.open()

    def _details_text(self, clip: Clip, mime_data: dict[str, bytes]) -> str:
        total_size = format_byte_size(sum(len(data) for data in mime_data.values()))
        if clip.kind in {"text", "html"}:
            stats = text_statistics(_clip_text(clip, mime_data))
            return self.tr(
                "{words} words · {characters} characters ({without_spaces} without spaces)"
                " · {lines} lines · {paragraphs} paragraphs · {size}"
            ).format(
                words=stats.words,
                characters=stats.characters,
                without_spaces=stats.characters_without_whitespace,
                lines=stats.lines,
                paragraphs=stats.paragraphs,
                size=total_size,
            )
        if clip.kind == "files":
            count = len([line for line in mime_data.get("text/uri-list", b"").splitlines() if line])
            return self.tr("{count} files · {size}").format(count=count, size=total_size)
        image = QImage.fromData(mime_data.get("image/png", b""), "PNG")
        dimensions = (
            self.tr("{width} × {height} px").format(width=image.width(), height=image.height())
            if not image.isNull()
            else self.tr("Unknown dimensions")
        )
        return self.tr("Image · {dimensions} · {size}").format(
            dimensions=dimensions, size=total_size
        )

    @staticmethod
    def _build_text_view(clip: Clip, mime_data: dict[str, bytes]) -> QPlainTextEdit:
        widget = QPlainTextEdit()
        widget.setPlainText(_clip_text(clip, mime_data))
        widget.setReadOnly(True)
        widget.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        return widget

    @staticmethod
    def _build_image_label(mime_data: dict[str, bytes]) -> QLabel:
        png = mime_data.get("image/png", b"")
        pixmap = QPixmap.fromImage(QImage.fromData(png, "PNG"))
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().size()
            # Leave room for window chrome/the Close button; a raw 1:1 dump
            # could exceed the screen for a full-desktop screenshot clip.
            max_size = QSize(int(available.width() * 0.9), int(available.height() * 0.8))
            if pixmap.width() > max_size.width() or pixmap.height() > max_size.height():
                pixmap = pixmap.scaled(
                    max_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        label = QLabel()
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return label


class EditDialog(QDialog):
    """Built-in modal editor for text clips -- Save/Cancel, no external process."""

    def __init__(self, text: str, parent=None, *, initial_query: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit"))
        layout = QVBoxLayout(self)

        self._editor = QPlainTextEdit()
        self._editor.setPlainText(text)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._find_bar = _FindBar(self._editor, self)
        self._find_bar.hide()
        layout.addWidget(self._find_bar)
        layout.addWidget(self._editor)
        QShortcut(QKeySequence.StandardKey.Find, self).activated.connect(self.show_find)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(_DEFAULT_SIZE)
        if initial_query.strip():
            self._find_bar.open(initial_query)
        else:
            self._editor.setFocus()

    def show_find(self) -> None:
        self._find_bar.open()

    def text(self) -> str:
        return self._editor.toPlainText()
