"""Qt dialog for staging clips before a composed clipboard action (F26)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from keeps.store import Store
from keeps.ui import text_transform
from keeps.workbench import (
    WorkbenchItem,
    WorkbenchResult,
    compose,
    move_item,
    remove_item,
    set_transform,
)


class WorkbenchDialog(QDialog):
    """A small, explicit tray for reorder/transform/preview/paste operations."""

    def __init__(
        self,
        store: Store,
        clip_ids: list[int],
        separator: str,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Clipboard Workbench"))
        self._store = store
        self._separator = separator
        self._all_clips = {clip.id: clip for clip in store.all()}
        self._items = [
            WorkbenchItem(
                self._all_clips[clip_id].id,
                self._all_clips[clip_id].kind,
                store.get_data(clip_id),
            )
            for clip_id in dict.fromkeys(clip_ids)
            if clip_id in self._all_clips
        ]
        self._result: WorkbenchResult | None = None
        self._action: str | None = None

        self._list = QListWidget()
        self._list.currentRowChanged.connect(self._on_current_changed)
        self._available = QListWidget()
        self._available.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        add_button = QPushButton(self.tr("Add selected"))
        add_button.clicked.connect(self._add_selected)

        self._transform = QComboBox()
        self._transform.addItem(self.tr("Original"), None)
        for label in text_transform.TRANSFORMS:
            self._transform.addItem(self.tr(label), label)
        self._transform.currentIndexChanged.connect(self._on_transform_changed)

        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setPlaceholderText(self.tr("The assembled plain-text result appears here."))
        self._status = QLabel()
        self._status.setWordWrap(True)

        up = QPushButton(self.tr("Move up"))
        up.clicked.connect(self._move_up)
        down = QPushButton(self.tr("Move down"))
        down.clicked.connect(self._move_down)
        remove = QPushButton(self.tr("Remove"))
        remove.clicked.connect(self._remove)
        controls = QHBoxLayout()
        controls.addWidget(up)
        controls.addWidget(down)
        controls.addWidget(remove)

        form = QHBoxLayout()
        form.addWidget(QLabel(self.tr("Transform selected:")))
        form.addWidget(self._transform, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        paste_button = buttons.addButton(
            self.tr("Paste"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        save_button = buttons.addButton(
            self.tr("Save as clip"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        buttons.rejected.connect(self.reject)
        paste_button.clicked.connect(lambda: self._finish("paste"))
        save_button.clicked.connect(lambda: self._finish("save"))

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(self.tr("Arrange clips, optionally transform text, then preview the result."))
        )
        columns = QHBoxLayout()
        staged = QVBoxLayout()
        staged.addWidget(QLabel(self.tr("Workbench")))
        staged.addWidget(self._list)
        staged.addLayout(controls)
        available = QVBoxLayout()
        available.addWidget(QLabel(self.tr("Available clips")))
        available.addWidget(self._available)
        available.addWidget(add_button)
        columns.addLayout(staged, 1)
        columns.addLayout(available, 1)
        layout.addLayout(columns)
        layout.addLayout(form)
        layout.addWidget(self._preview)
        layout.addWidget(self._status)
        layout.addWidget(buttons)
        self.resize(620, 520)
        self._rebuild()

    @property
    def action(self) -> str | None:
        return self._action

    def result(self) -> WorkbenchResult | None:
        return self._result

    def _current_row(self) -> int:
        return self._list.currentRow()

    def _on_current_changed(self, row: int) -> None:
        self._transform.blockSignals(True)
        try:
            self._transform.setCurrentIndex(
                max(0, self._transform.findData(self._items[row].transform))
                if 0 <= row < len(self._items)
                else 0
            )
            has_plain = (
                0 <= row < len(self._items)
                and "text/plain" in self._items[row].mime_data
            )
            for index in range(1, self._transform.count()):
                self._transform.model().item(index).setEnabled(has_plain)
        finally:
            self._transform.blockSignals(False)

    def _on_transform_changed(self, _index: int) -> None:
        row = self._current_row()
        if not 0 <= row < len(self._items):
            return
        transform = self._transform.currentData()
        try:
            self._items = list(set_transform(self._items, row, transform))
        except ValueError:
            return
        self._rebuild(select_row=row)

    def _move_up(self) -> None:
        self._move(-1)

    def _move_down(self) -> None:
        self._move(1)

    def _move(self, direction: int) -> None:
        row = self._current_row()
        if not 0 <= row < len(self._items):
            return
        self._items = list(move_item(self._items, row, direction))
        self._rebuild(select_row=max(0, min(len(self._items) - 1, row + direction)))

    def _remove(self) -> None:
        row = self._current_row()
        if not 0 <= row < len(self._items):
            return
        self._items = list(remove_item(self._items, row))
        self._rebuild(select_row=min(row, len(self._items) - 1))

    def _add_selected(self) -> None:
        existing = {item.clip_id for item in self._items}
        selected_ids = [
            int(item.data(Qt.ItemDataRole.UserRole)) for item in self._available.selectedItems()
        ]
        added = [
            WorkbenchItem(
                self._all_clips[clip_id].id,
                self._all_clips[clip_id].kind,
                self._store.get_data(clip_id),
            )
            for clip_id in selected_ids
            if clip_id not in existing
        ]
        if not added:
            return
        self._items.extend(added)
        self._rebuild(select_row=len(self._items) - 1)

    def _rebuild(self, select_row: int | None = None) -> None:
        self._list.blockSignals(True)
        try:
            self._list.clear()
            for item in self._items:
                label = self.tr("#{id} · {kind}").format(
                    id=item.clip_id, kind=self.tr(item.kind.upper())
                )
                if item.transform:
                    label += self.tr(" · {transform}").format(
                        transform=self.tr(item.transform)
                    )
                widget_item = QListWidgetItem(label)
                widget_item.setData(Qt.ItemDataRole.UserRole, item.clip_id)
                self._list.addItem(widget_item)
        finally:
            self._list.blockSignals(False)
        if self._items:
            self._list.setCurrentRow(
                min(len(self._items) - 1, select_row if select_row is not None else 0)
            )
        existing = {item.clip_id for item in self._items}
        self._available.clear()
        for clip in self._all_clips.values():
            if clip.id in existing:
                continue
            label = self.tr("#{id} · {kind}").format(
                id=clip.id, kind=self.tr(clip.kind.upper())
            )
            available_item = QListWidgetItem(label)
            available_item.setData(Qt.ItemDataRole.UserRole, clip.id)
            self._available.addItem(available_item)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        self._result = compose(self._items, self._separator, text_transform.TRANSFORMS)
        result = self._result
        if result is None:
            self._preview.clear()
            self._status.setText(self.tr("No pasteable content in the workbench."))
            return
        text = result.mime_data.get("text/plain")
        if text is None:
            self._preview.setPlainText(
                self.tr("Single non-text clip — its original MIME formats will be pasted.")
            )
        else:
            self._preview.setPlainText(text.decode("utf-8", errors="replace"))
        status = self.tr("{count} clip(s) included.").format(count=len(result.included_ids))
        if result.skipped_ids:
            status += " " + self.tr("Skipped {count} clip(s) without plain text.").format(
                count=len(result.skipped_ids)
            )
        self._status.setText(status)

    def _finish(self, action: str) -> None:
        if self.result() is None:
            QMessageBox.warning(
                self,
                self.tr("Nothing to do"),
                self.tr("Add at least one clip with usable content."),
            )
            return
        self._action = action
        self.accept()
