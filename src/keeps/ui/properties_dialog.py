"""Clip metadata and alias editor (PLAN.md Ф17)."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from keeps.store import Clip


def _format_time(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


class PropertiesDialog(QDialog):
    def __init__(
        self,
        clip: Clip,
        mime_sizes: list[tuple[str, int]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Clip Properties"))
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self._alias = QLineEdit(clip.alias or "")
        form.addRow(self.tr("Title (alias)"), self._alias)
        self._pinned = QCheckBox()
        self._pinned.setChecked(clip.pinned)
        form.addRow(self.tr("Pinned"), self._pinned)
        form.addRow(self.tr("Created"), QLabel(_format_time(clip.created_at)))
        form.addRow(self.tr("Last used"), QLabel(_format_time(clip.last_used_at)))
        form.addRow(self.tr("Use count"), QLabel(str(clip.use_count)))
        form.addRow(self.tr("Kind"), QLabel(clip.kind))
        hash_label = QLabel(clip.hash)
        hash_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        form.addRow(self.tr("SHA-256"), hash_label)
        layout.addLayout(form)

        mime_view = QPlainTextEdit()
        mime_view.setReadOnly(True)
        mime_view.setPlainText("\n".join(f"{mime}: {size} bytes" for mime, size in mime_sizes))
        layout.addWidget(mime_view)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(560, 420)

    def alias(self) -> str:
        return self._alias.text().strip()

    def pinned(self) -> bool:
        return self._pinned.isChecked()
