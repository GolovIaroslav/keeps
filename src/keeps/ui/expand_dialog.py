"""Ф9.2: read-only "View" expand + built-in "Edit" dialogs for a selected clip.

Separate from the existing external-editor flow (ui/popup.py::_edit_current,
Ctrl+E, xdg-open+temp-file+QFileSystemWatcher) which is unrelated and unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPixmap, QShortcut
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

from keeps.store import Clip
from keeps.ui.format import format_byte_size, text_statistics

_DEFAULT_SIZE = QSize(480, 400)


class _FindBar(QWidget):
    """Small Ctrl+F bar backed by QTextDocument's case-insensitive search."""

    def __init__(self, editor: QPlainTextEdit, parent=None) -> None:
        super().__init__(parent)
        self._editor = editor
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
        cursor = self._editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self._find_from(cursor, backward=False)

    def find_next(self) -> None:
        self._find_from(self._editor.textCursor(), backward=False)

    def find_previous(self) -> None:
        cursor = self._editor.textCursor()
        cursor.setPosition(cursor.selectionStart())
        self._find_from(cursor, backward=True)

    def _find_from(self, cursor, *, backward: bool) -> None:
        query = self._query.text()
        if not query:
            self._editor.setExtraSelections([])
            self._counter.setText("0 / 0")
            return
        flag = (
            self._editor.document().FindFlag.FindBackward
            if backward
            else self._editor.document().FindFlag(0)
        )
        match = self._editor.document().find(query, cursor, flag)
        if match.isNull():
            cursor.movePosition(
                cursor.MoveOperation.End if backward else cursor.MoveOperation.Start
            )
            match = self._editor.document().find(query, cursor, flag)
        if match.isNull():
            self._editor.setExtraSelections([])
            self._counter.setText("0 / 0")
            return

        self._editor.setTextCursor(match)
        self._editor.ensureCursorVisible()
        highlight = QTextEdit.ExtraSelection()
        highlight.cursor = match
        highlight.format.setBackground(QColor("#f6c343"))
        highlight.format.setForeground(QColor("#171717"))
        self._editor.setExtraSelections([highlight])

        text = self._editor.toPlainText()
        folded_query = query.casefold()
        total = text.casefold().count(folded_query)
        current = text[: match.selectionStart()].casefold().count(folded_query) + 1
        self._counter.setText(f"{current} / {total}")


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
