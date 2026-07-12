"""Ф9.2: read-only "View" expand + built-in "Edit" dialogs for a selected clip.

Separate from the existing external-editor flow (ui/popup.py::_edit_current,
Ctrl+E, xdg-open+temp-file+QFileSystemWatcher) which is unrelated and unchanged.
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from keeps.store import Clip

_DEFAULT_SIZE = QSize(480, 400)


def _clip_text(clip: Clip, mime_data: dict[str, bytes]) -> str:
    """Full text for a clip, per-kind, mirroring store.py's build_preview() dispatch."""
    if clip.kind == "files":
        raw = mime_data.get("text/uri-list", b"")
    else:
        raw = mime_data.get("text/plain") or mime_data.get("text/html", b"")
    return raw.decode("utf-8", errors="replace")


class ViewDialog(QDialog):
    """Read-only expand: full wrapped text, or the image at (up to) full size."""

    def __init__(self, clip: Clip, mime_data: dict[str, bytes], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("View"))
        layout = QVBoxLayout(self)

        if clip.kind == "image":
            layout.addWidget(self._build_image_label(mime_data))
            if clip.ocr_text and clip.ocr_text.strip():
                ocr_view = QPlainTextEdit()
                ocr_view.setPlainText(clip.ocr_text)
                ocr_view.setReadOnly(True)
                ocr_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
                layout.addWidget(ocr_view)
        else:
            layout.addWidget(self._build_text_view(clip, mime_data))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self.resize(_DEFAULT_SIZE)

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

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Edit"))
        layout = QVBoxLayout(self)

        self._editor = QPlainTextEdit()
        self._editor.setPlainText(text)
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        layout.addWidget(self._editor)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.resize(_DEFAULT_SIZE)
        self._editor.setFocus()

    def text(self) -> str:
        return self._editor.toPlainText()
