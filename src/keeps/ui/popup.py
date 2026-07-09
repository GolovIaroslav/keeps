"""Popup window: search-as-you-type list of clips, keymap per PLAN.md §6."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QEvent,
    QFileSystemWatcher,
    QMimeData,
    QModelIndex,
    QObject,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QImage, QKeyEvent
from PySide6.QtWidgets import QAbstractItemView, QLineEdit, QListView, QVBoxLayout, QWidget

from keeps.store import Clip, Store
from keeps.ui.delegate import ClipItemDelegate

SEARCH_DEBOUNCE_MS = 50
DEFAULT_SIZE = QSize(420, 480)

# Ctrl+E scope decision (Ф3): only plain "text" clips are editable in an
# external editor for now — editing html/image/files has no clean semantics
# yet (see implementation-notes.md).
EDITABLE_KINDS = {"text"}

_NAV_KEYS = {
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
}


def _settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = config_home / "keeps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "keeps.ini"


class ClipListModel(QAbstractListModel):
    def __init__(self, store: Store, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._clips: list[Clip] = []

    def set_query(self, query: str) -> None:
        self.beginResetModel()
        self._clips = self._store.search(query)
        self.endResetModel()

    def clip_at(self, row: int) -> Clip:
        return self._clips[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._clips)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self._clips[index.row()].preview
        return None


class PopupWindow(QWidget):
    # Emitted after a clip is copied to the clipboard with paste intent
    # (Enter/Shift+Enter/Ctrl+1..9); Ф4 connects the actual ydotool/xdotool
    # injection here. Ctrl+C copies without emitting this.
    paste_requested = Signal(int, bool)  # (clip_id, plain_only)

    def __init__(self, store: Store, parent: QWidget | None = None) -> None:
        # Qt.WindowType.Popup would give us free hide-on-outside-click, but its
        # Wayland grab requires a focused transient parent — we have none (a
        # global hotkey triggers us, not another widget). So: a plain
        # frameless top-level window with manual focus-out handling instead.
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.store = store
        self.model = ClipListModel(store)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("Search clips..."))
        self.search_edit.installEventFilter(self)

        self.list_view = QListView(self)
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(ClipItemDelegate(store, self.list_view))
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.doubleClicked.connect(self._on_double_clicked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.search_edit)
        layout.addWidget(self.list_view)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_filter)
        self.search_edit.textChanged.connect(lambda _text: self._debounce.start())

        self._edit_watcher = QFileSystemWatcher(self)
        self._edit_watcher.fileChanged.connect(self._on_edited_file_changed)
        self._edit_sessions: dict[str, int] = {}

        self._settings = QSettings(str(_settings_path()), QSettings.Format.IniFormat)
        size = self._settings.value("popup/size")
        self.resize(size if size is not None else DEFAULT_SIZE)

    # -- lifecycle ---------------------------------------------------------

    def show_popup(self) -> None:
        self.search_edit.clear()
        self.refresh()
        self._select_row(0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.search_edit.setFocus()

    def hideEvent(self, event) -> None:
        self._settings.setValue("popup/size", self.size())
        super().hideEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            self.hide()
        return super().event(event)

    def refresh(self) -> None:
        self.model.set_query(self.search_edit.text())

    def _apply_filter(self) -> None:
        self.refresh()
        self._select_row(0)

    def _select_row(self, row: int) -> None:
        if 0 <= row < self.model.rowCount():
            self.list_view.setCurrentIndex(self.model.index(row, 0))

    def _current_row(self) -> int | None:
        index = self.list_view.currentIndex()
        return index.row() if index.isValid() else None

    # -- keymap (PLAN.md §6), paste injection itself is Ф4 -----------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.search_edit and event.type() == QEvent.Type.KeyPress:
            return self._handle_key(event)
        return super().eventFilter(obj, event)

    def _handle_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        mods = event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        if key in _NAV_KEYS:
            QCoreApplication.sendEvent(self.list_view, event)
            return True
        if key == Qt.Key.Key_Escape:
            self.hide()
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = self._current_row()
            if row is not None:
                self._activate(row, plain_only=shift, want_paste=True)
            return True
        if ctrl and key == Qt.Key.Key_C:
            row = self._current_row()
            if row is not None:
                self._activate(row, plain_only=False, want_paste=False)
            return True
        if key == Qt.Key.Key_Delete:
            self._delete_current()
            return True
        if ctrl and key == Qt.Key.Key_E:
            self._edit_current()
            return True
        if ctrl and key == Qt.Key.Key_P:
            self._toggle_pin_current()
            return True
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            row = key - Qt.Key.Key_1
            if row < self.model.rowCount():
                self._activate(row, plain_only=False, want_paste=True)
            return True
        return False

    def _on_double_clicked(self, index: QModelIndex) -> None:
        self._activate(index.row(), plain_only=False, want_paste=True)

    # -- actions -------------------------------------------------------------

    def _activate(self, row: int, plain_only: bool, want_paste: bool) -> None:
        clip = self.model.clip_at(row)
        mime_data = self.store.get_data(clip.id)
        self._set_clipboard(mime_data, plain_only)
        self.store.touch(clip.id)
        self.hide()
        if want_paste:
            self.paste_requested.emit(clip.id, plain_only)

    @staticmethod
    def _set_clipboard(mime_data: dict[str, bytes], plain_only: bool) -> None:
        qmime = QMimeData()
        plain = mime_data.get("text/plain")
        if plain is not None:
            qmime.setText(plain.decode("utf-8", errors="replace"))
        if not plain_only:
            html = mime_data.get("text/html")
            if html is not None:
                qmime.setHtml(html.decode("utf-8", errors="replace"))
            png = mime_data.get("image/png")
            if png is not None:
                image = QImage.fromData(png, "PNG")
                qmime.setImageData(image)
            uri_list = mime_data.get("text/uri-list")
            if uri_list is not None:
                urls = [QUrl(line) for line in uri_list.decode("utf-8").splitlines() if line]
                qmime.setUrls(urls)
        QGuiApplication.clipboard().setMimeData(qmime)

    def _delete_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        self.store.delete(clip.id)
        self.refresh()
        self._select_row(min(row, self.model.rowCount() - 1))

    def _toggle_pin_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        self.store.set_pinned(clip.id, not clip.pinned)
        self.refresh()
        self._select_row(min(row, self.model.rowCount() - 1))

    def _edit_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        if clip.kind not in EDITABLE_KINDS:
            return
        text = self.store.get_data(clip.id).get("text/plain", b"").decode("utf-8", errors="replace")
        fd, path = tempfile.mkstemp(prefix="keeps-edit-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self._edit_sessions[path] = clip.id
        self._edit_watcher.addPath(path)
        subprocess.Popen(["xdg-open", path])

    def _on_edited_file_changed(self, path: str) -> None:
        clip_id = self._edit_sessions.get(path)
        if clip_id is None or not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            new_text = f.read()
        self.store.update_content(clip_id, {"text/plain": new_text.encode("utf-8")})
        self.refresh()
        if path not in self._edit_watcher.files():
            self._edit_watcher.addPath(path)  # some editors atomic-save (rm+recreate)
