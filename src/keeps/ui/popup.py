"""Popup window: search-as-you-type list of clips, keymap per PLAN.md §6."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QEvent,
    QFileSystemWatcher,
    QMimeData,
    QModelIndex,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QGuiApplication, QIcon, QImage, QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from keeps import config, paste
from keeps.ai import ranking
from keeps.ai.runtime import AiRuntime
from keeps.store import Clip, Store
from keeps.ui import geometry, text_transform
from keeps.ui.delegate import ClipItemDelegate
from keeps.ui.expand_dialog import EditDialog, ViewDialog
from keeps.ui.settings import SettingsDialog

SEARCH_DEBOUNCE_MS = 50
DEFAULT_SIZE = QSize(420, 480)

# Ф9.4: thin drag handle at the top of the popup, since a frameless window
# has no native title bar to grab -- see _TitleBar below.
_TITLE_BAR_HEIGHT = 22

# Frameless windows have no native resize grip; this is both the hit-test
# margin for drag-resize (mousePressEvent/mouseMoveEvent below) and the
# layout's content margin, so that ring always belongs to the window itself
# rather than a child widget (search_edit/list_view) swallowing the event.
_RESIZE_MARGIN = geometry.RESIZE_MARGIN

_EDGE_TO_QT = {
    "left": Qt.Edge.LeftEdge,
    "right": Qt.Edge.RightEdge,
    "top": Qt.Edge.TopEdge,
    "bottom": Qt.Edge.BottomEdge,
}

_EDGE_CURSORS = {
    frozenset({"left"}): Qt.CursorShape.SizeHorCursor,
    frozenset({"right"}): Qt.CursorShape.SizeHorCursor,
    frozenset({"top"}): Qt.CursorShape.SizeVerCursor,
    frozenset({"bottom"}): Qt.CursorShape.SizeVerCursor,
    frozenset({"top", "left"}): Qt.CursorShape.SizeFDiagCursor,
    frozenset({"bottom", "right"}): Qt.CursorShape.SizeFDiagCursor,
    frozenset({"top", "right"}): Qt.CursorShape.SizeBDiagCursor,
    frozenset({"bottom", "left"}): Qt.CursorShape.SizeBDiagCursor,
}

# F2's built-in editor remains text-only; Ctrl+E's external editor supports
# the three content kinds that have a corresponding file representation.
EDITABLE_KINDS = {"text"}
EXTERNAL_EDIT_KINDS = {"text", "html", "image"}

_NAV_KEYS = {
    Qt.Key.Key_Up,
    Qt.Key.Key_Down,
    Qt.Key.Key_PageUp,
    Qt.Key.Key_PageDown,
    Qt.Key.Key_Home,
    Qt.Key.Key_End,
}

_MODE_BADGE_LABELS = {
    ranking.SearchMode.BLENDED: "blended",
    ranking.SearchMode.KEYWORD: "keywords",
    ranking.SearchMode.SEMANTIC: "meaning",
}

# Clicking the badge cycles modes the same way Ctrl+M does (PLAN.md §9);
# the tooltip exists because "blended"/"keywords"/"meaning" alone read as
# unexplained jargon otherwise (user feedback 2026-07-11).
_MODE_BADGE_TOOLTIPS = {
    ranking.SearchMode.BLENDED: (
        "Auto (default): exact matches first, semantically related results "
        "blended in below them. Click to switch mode, or Ctrl+M."
    ),
    ranking.SearchMode.KEYWORD: (
        "Keywords only: exact substring matches, no semantic ranking. "
        "Click to switch mode, or Ctrl+M."
    ),
    ranking.SearchMode.SEMANTIC: (
        "Meaning only: ranked purely by semantic similarity to your query, "
        "ignoring exact matches. Click to switch mode, or Ctrl+M."
    ),
}


class ClipListModel(QAbstractListModel):
    def __init__(
        self, store: Store, ai_runtime: AiRuntime | None = None, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._ai_runtime = ai_runtime
        self._clips: list[Clip] = []
        self._current_query = ""
        self._semantic_scores: dict[int, int] = {}
        # Ф9.3: ids of every clip *pasted* (not merely copied) this popup
        # session, all highlighted by the delegate simultaneously (user
        # request 2026-07-12: pasting several different clips in a row should
        # keep all of them marked, not just the most recent one). Session-
        # local, not persisted; survives _rebuild() since it lives outside
        # self._clips and is looked up by id, never by row index.
        self._pasted_ids: set[int] = set()

    @property
    def pasted_ids(self) -> frozenset[int]:
        return frozenset(self._pasted_ids)

    def mark_pasted(self, clip_id: int) -> None:
        self._pasted_ids.add(clip_id)

    def set_query(self, query: str) -> None:
        self._current_query = query
        self._semantic_scores = {}
        self._rebuild()
        if self._ai_runtime is not None and self._ai_runtime.rag_text_enabled and query.strip():
            self._ai_runtime.encode_query_async(query, self._on_semantic_scores)

    def _on_semantic_scores(self, query: str, scores: dict[int, float]) -> None:
        if query != self._current_query:
            return  # a newer keystroke already superseded this search
        self._semantic_scores = scores
        self._rebuild()

    def _rebuild(self) -> None:
        substring_clips = self._store.search(self._current_query)
        rag_active = (
            self._ai_runtime is not None
            and self._ai_runtime.rag_text_enabled
            and bool(self._current_query.strip())
        )
        if rag_active:
            clips_by_id = {clip.id: clip for clip in self._store.all()}
            clips = ranking.blend(
                substring_clips,
                self._semantic_scores,
                clips_by_id,
                mode=self._ai_runtime.search_mode,
            )
        else:
            clips = substring_clips
        self.beginResetModel()
        self._clips = clips
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


class _PasteInjectionTask(QRunnable):
    """Runs ydotool/xdotool off the main thread.

    The main thread owns the clipboard right after _set_clipboard, so it must
    stay responsive to serve paste requests from other apps -- a subprocess
    hang here (ydotoold under load, PLAN.md §11) must never block it.
    paste.inject_paste() also carries its own timeout as defense in depth.
    """

    def __init__(self, backend: str) -> None:
        super().__init__()
        self._backend = backend

    def run(self) -> None:
        if not paste.inject_paste(self._backend, shutil.which, subprocess.run):
            paste.notify_paste_unavailable(self._backend, shutil.which)


class _TitleBar(QWidget):
    """Thin drag handle at the top of the popup (Ф9.4).

    A frameless window has no native title bar to grab, so this widget's own
    mousePressEvent starts a compositor-driven move -- the same
    startSystemMove/startSystemResize mechanism PopupWindow's own
    mousePressEvent already uses for edge drag-resize. The settings button is
    a proper child widget and consumes its own press before it would reach
    here, so clicking it just clicks rather than starting a window drag.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(_TITLE_BAR_HEIGHT)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        window = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and window is not None:
            if window.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)


class PopupWindow(QWidget):
    # Emitted after a clip is copied to the clipboard with paste intent
    # (Enter/Shift+Enter/Ctrl+1..9); connected below to the actual
    # ydotool/xdotool injection (keeps.paste), after paste/delay_ms. Ctrl+C
    # copies without emitting this.
    paste_requested = Signal(int, bool)  # (clip_id, plain_only)
    thumbnail_requested = Signal(int, str)  # (clip_id, kind), after an image edit

    def __init__(
        self, store: Store, ai_runtime: AiRuntime | None = None, parent: QWidget | None = None
    ) -> None:
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
        self.setMouseTracking(True)  # hover cursor feedback near the resize margin
        self.store = store
        self._ai_runtime = ai_runtime
        self.model = ClipListModel(store, ai_runtime)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("Search clips..."))
        self.search_edit.installEventFilter(self)

        # Search-mode badge (Ctrl+M cycles blended/keywords/meaning) -- only
        # shown when ai/rag_text_enabled, since there's nothing to switch
        # between otherwise (PLAN.md §9).
        self._mode_badge = QLabel(self)
        self._mode_badge.setVisible(False)
        self._mode_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_badge.installEventFilter(self)

        self.list_view = QListView(self)
        self.list_view.setModel(self.model)
        self._delegate = ClipItemDelegate(store, self.list_view)
        self.list_view.setItemDelegate(self._delegate)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.doubleClicked.connect(self._on_double_clicked)
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        # Wheel events over the list land on its viewport, not list_view itself
        # (QAbstractScrollArea plumbing) -- filter there for Ctrl+wheel scaling.
        self.list_view.viewport().installEventFilter(self)
        # Clicking a clip moves keyboard focus from search_edit to the list
        # itself; the popup keymap (F2/F3/Ctrl+P/...) must keep working from
        # there too, so filter the list's key events as well.
        self.list_view.installEventFilter(self)

        search_row = QHBoxLayout()
        search_row.addWidget(self.search_edit)
        search_row.addWidget(self._mode_badge)

        self._title_bar = _TitleBar(self)
        title_bar_layout = QHBoxLayout(self._title_bar)
        title_bar_layout.setContentsMargins(2, 0, 2, 0)
        title_bar_layout.addStretch(1)
        settings_button = QToolButton(self._title_bar)
        settings_button.setIcon(QIcon.fromTheme("configure"))
        settings_button.setToolTip(self.tr("Settings..."))
        settings_button.setAutoRaise(True)
        settings_button.clicked.connect(self._open_settings)
        title_bar_layout.addWidget(settings_button)

        layout = QVBoxLayout(self)
        # Matches _RESIZE_MARGIN so the drag-resize hit-test ring is never
        # covered by a child widget (search_edit/list_view).
        layout.setContentsMargins(*([_RESIZE_MARGIN] * 4))
        layout.addWidget(self._title_bar)
        layout.addLayout(search_row)
        layout.addWidget(self.list_view)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_filter)
        self.search_edit.textChanged.connect(lambda _text: self._debounce.start())

        self._edit_watcher = QFileSystemWatcher(self)
        self._edit_watcher.fileChanged.connect(self._on_edited_file_changed)
        self._edit_sessions: dict[str, tuple[int, str]] = {}

        self._settings = config.open_settings()
        size = self._settings.value("popup/size")
        self.resize(size if size is not None else DEFAULT_SIZE)

        # Ctrl+scroll / Ctrl+Plus / Ctrl+Minus (§6): scales the whole popup UI
        # (font + delegate thumbnail/padding), not just text, and is
        # remembered across sessions the same way popup/size is.
        self._base_point_size = self.font().pointSizeF()
        self._ui_scale = float(self._settings.value("popup/ui_scale", 1.0))
        self._apply_ui_scale()

        self.paste_requested.connect(self._schedule_paste_injection)

    # -- lifecycle ---------------------------------------------------------

    def show_popup(self) -> None:
        self.search_edit.clear()
        self._update_mode_badge()
        self.refresh()
        self._select_row(0)
        self._drop_stale_surface()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search_edit.setFocus()

    def toggle_popup(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_popup()

    def _drop_stale_surface(self) -> None:
        """Discard a native window bound to a screen that no longer exists.

        A daemon autostarted in the first seconds of a Plasma Wayland session
        can see only Qt's placeholder screen; a window created against it is
        never mapped by the compositor and show() then fails silently forever
        (QTBUG-98010 family). Destroying the native window makes show()
        recreate the surface on the real, current screen. No-op normally.
        """
        handle = self.windowHandle()
        if handle is None:
            return
        if handle.screen() not in QGuiApplication.screens():
            print(
                "keeps: popup window was bound to a dead screen; recreating surface",
                file=sys.stderr,
            )
            self.destroy()

    def hideEvent(self, event) -> None:
        self._settings.setValue("popup/size", self.size())
        super().hideEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            # Opening one of our own modal dialogs (F2/F3/Settings) also
            # deactivates the popup; hiding it then strands the dialog with
            # no parent window under it (possibly on another screen) and the
            # popup looks "hung" behind the invisible modal grab.
            if QGuiApplication.modalWindow() is None:
                self.hide()
        return super().event(event)

    def refresh(self) -> None:
        self.model.set_query(self.search_edit.text())
        self._delegate.prune_thumbnail_cache({clip.id for clip in self.store.all()})

    def on_thumbnail_ready(self, clip_id: int) -> None:
        self._delegate.invalidate_thumbnail(clip_id)
        self.list_view.viewport().update()

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
        if event.type() == QEvent.Type.Wheel and self._handle_wheel(event):
            return True
        if obj is self.search_edit and event.type() == QEvent.Type.KeyPress:
            return self._handle_key(event)
        if obj is self.list_view and event.type() == QEvent.Type.KeyPress:
            if event.key() in _NAV_KEYS:
                # QListView's own navigation already handles these; falling
                # through to _handle_key would sendEvent() the event straight
                # back to list_view and recurse through this filter.
                return False
            return self._handle_key(event)
        if obj is self._mode_badge and event.type() == QEvent.Type.MouseButtonPress:
            self._cycle_search_mode()
            return True
        # A resize-edge cursor set by mouseMoveEvent (near the window border,
        # see below) otherwise sticks once the mouse crosses into a child
        # widget -- Qt doesn't re-deliver mouseMoveEvent to this window once
        # a child owns the pointer, so unsetCursor() there never runs.
        if event.type() == QEvent.Type.Enter and obj in (
            self.search_edit,
            self.list_view.viewport(),
        ):
            self.unsetCursor()
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
        if key == Qt.Key.Key_F3:
            self._view_current()
            return True
        if key == Qt.Key.Key_F2:
            self._edit_builtin_current()
            return True
        if ctrl and key == Qt.Key.Key_P:
            self._toggle_pin_current()
            return True
        if ctrl and key == Qt.Key.Key_M:
            self._cycle_search_mode()
            return True
        if ctrl and key in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._set_ui_scale(geometry.next_ui_scale(self._ui_scale, 1))
            return True
        if ctrl and key == Qt.Key.Key_Minus:
            self._set_ui_scale(geometry.next_ui_scale(self._ui_scale, -1))
            return True
        if ctrl and Qt.Key.Key_1 <= key <= Qt.Key.Key_9:
            row = key - Qt.Key.Key_1
            if row < self.model.rowCount():
                self._activate(row, plain_only=False, want_paste=True)
            return True
        return False

    def _on_double_clicked(self, index: QModelIndex) -> None:
        self._activate(index.row(), plain_only=False, want_paste=True)

    # -- context menu (PLAN.md §6: "duplicates paste/paste as text/copy/pin/
    # edit/delete") + Special Paste submenu (Ф9.1, menu-only, no shortcut) ---

    def _show_context_menu(self, pos) -> None:
        index = self.list_view.indexAt(pos)
        if not index.isValid():
            return
        row = index.row()
        self._select_row(row)  # right-click on a non-selected row selects it first
        clip = self.model.clip_at(row)

        menu = QMenu(self.list_view)
        menu.addAction(
            self.tr("Paste"), lambda: self._activate(row, plain_only=False, want_paste=True)
        )
        menu.addAction(
            self.tr("Paste as text"),
            lambda: self._activate(row, plain_only=True, want_paste=True),
        )
        menu.addAction(
            self.tr("Copy"), lambda: self._activate(row, plain_only=False, want_paste=False)
        )
        if clip.kind == "image" and clip.ocr_text and clip.ocr_text.strip():
            menu.addAction(self.tr("Copy recognized text"), lambda: self._copy_ocr_text(row))
        menu.addSeparator()
        menu.addAction(self.tr("View"), self._view_current)
        pin_label = self.tr("Unpin") if clip.pinned else self.tr("Pin")
        menu.addAction(pin_label, self._toggle_pin_current)
        builtin_edit_action = menu.addAction(self.tr("Edit"), self._edit_builtin_current)
        builtin_edit_action.setEnabled(clip.kind in EDITABLE_KINDS)
        edit_action = menu.addAction(self.tr("Edit externally"), self._edit_current)
        edit_action.setEnabled(clip.kind in EXTERNAL_EDIT_KINDS)
        menu.addAction(self.tr("Delete"), self._delete_current)

        plain_text = self.store.get_data(clip.id).get("text/plain")
        if plain_text:
            menu.addSeparator()
            special_menu = menu.addMenu(self.tr("Special Paste"))
            for label, transform in text_transform.TRANSFORMS.items():
                special_menu.addAction(label, lambda t=transform: self._special_paste(row, t))

        menu.exec(self.list_view.viewport().mapToGlobal(pos))

    def _copy_ocr_text(self, row: int) -> None:
        clip = self.model.clip_at(row)
        self._set_clipboard({"text/plain": clip.ocr_text.encode("utf-8")}, plain_only=True)
        self.store.touch(clip.id)
        self.hide()

    def _special_paste(self, row: int, transform) -> None:
        """Paste a transformed copy of a clip's plain text without touching storage.

        Ditto-style Special Paste: the transform only affects what lands on
        the clipboard/target app, never the stored clip (no update_content
        call), same as the source clip content shown in the list afterwards.
        """
        clip = self.model.clip_at(row)
        plain = (
            self.store.get_data(clip.id).get("text/plain", b"").decode("utf-8", errors="replace")
        )
        transformed = transform(plain)
        self._set_clipboard({"text/plain": transformed.encode("utf-8")}, plain_only=True)
        self.store.touch(clip.id)
        self.model.mark_pasted(clip.id)
        self.hide()
        self.paste_requested.emit(clip.id, True)

    # -- actions -------------------------------------------------------------

    def _activate(self, row: int, plain_only: bool, want_paste: bool) -> None:
        clip = self.model.clip_at(row)
        mime_data = self.store.get_data(clip.id)
        self._set_clipboard(mime_data, plain_only)
        self.store.touch(clip.id)
        self.hide()
        if want_paste:
            self.model.mark_pasted(clip.id)
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

    def _schedule_paste_injection(self, clip_id: int, plain_only: bool) -> None:
        # Plain-vs-rich is already decided by what's on the clipboard
        # (_set_clipboard above); injection just replays Ctrl+V.
        del clip_id, plain_only
        if not config.get(self._settings, "paste/enabled"):
            return
        delay_ms = int(config.get(self._settings, "paste/delay_ms"))
        backend = paste.session_backend()
        QTimer.singleShot(delay_ms, lambda: self._run_paste_injection(backend))

    @staticmethod
    def _run_paste_injection(backend: str) -> None:
        QThreadPool.globalInstance().start(_PasteInjectionTask(backend))

    def _delete_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        self._delegate.invalidate_thumbnail(clip.id)
        self.store.delete(clip.id)
        self.refresh()
        self._select_row(min(row, self.model.rowCount() - 1))

    def _open_settings(self) -> None:
        # Mirrors app.py's on_settings_requested (tray path) exactly, so
        # Settings behaves identically whether opened from the tray or here.
        SettingsDialog(self._ai_runtime, self.store).exec()
        self.refresh()
        # Qt doesn't reliably restore focus to search_edit after a modal
        # dialog closes; _handle_key only fires for events targeting
        # search_edit specifically, so without this the whole keymap
        # (F2/F3/Ctrl+P/...) silently stops responding until the user
        # clicks back into the search box.
        self.search_edit.setFocus()

    def _cycle_search_mode(self) -> None:
        if self._ai_runtime is None or not self._ai_runtime.rag_text_enabled:
            return  # nothing to switch between when RAG is off (PLAN.md §9)
        self._ai_runtime.search_mode = self._ai_runtime.search_mode.next()
        self._update_mode_badge()
        self.refresh()

    def _update_mode_badge(self) -> None:
        rag_on = self._ai_runtime is not None and self._ai_runtime.rag_text_enabled
        self._mode_badge.setVisible(rag_on)
        if rag_on:
            mode = self._ai_runtime.search_mode
            self._mode_badge.setText(f"[{_MODE_BADGE_LABELS[mode]}]")
            self._mode_badge.setToolTip(_MODE_BADGE_TOOLTIPS[mode])

    # -- UI scale (Ctrl+scroll / Ctrl+Plus / Ctrl+Minus, §6) ----------------

    def _handle_wheel(self, event: QWheelEvent) -> bool:
        if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            return False
        direction = 1 if event.angleDelta().y() > 0 else -1
        self._set_ui_scale(geometry.next_ui_scale(self._ui_scale, direction))
        return True

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._handle_wheel(event):
            super().wheelEvent(event)

    def _set_ui_scale(self, scale: float) -> None:
        if scale == self._ui_scale:
            return
        self._ui_scale = scale
        self._apply_ui_scale()

    def _apply_ui_scale(self) -> None:
        font = self.font()
        font.setPointSizeF(self._base_point_size * self._ui_scale)
        self.setFont(font)
        self._delegate.set_scale(self._ui_scale)
        self.list_view.doItemsLayout()
        self._settings.setValue("popup/ui_scale", self._ui_scale)

    # -- drag-resize by any edge/corner (frameless window has no native grip)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        edges = geometry.resize_edges(pos.x(), pos.y(), self.width(), self.height())
        if edges:
            self.setCursor(_EDGE_CURSORS[edges])
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle() is not None:
            pos = event.position().toPoint()
            edges = geometry.resize_edges(pos.x(), pos.y(), self.width(), self.height())
            if edges:
                qt_edges = Qt.Edge(0)
                for name in edges:
                    qt_edges |= _EDGE_TO_QT[name]
                if self.windowHandle().startSystemResize(qt_edges):
                    event.accept()
                    return
        super().mousePressEvent(event)

    def _toggle_pin_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        self.store.set_pinned(clip.id, not clip.pinned)
        self.refresh()
        self._select_row(min(row, self.model.rowCount() - 1))

    def _view_current(self) -> None:
        """F3, any kind: read-only expand (Ditto's "View Full Description")."""
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        mime_data = self.store.get_data(clip.id)
        ViewDialog(clip, mime_data, self).exec()
        self.search_edit.setFocus()

    def _edit_builtin_current(self) -> None:
        """F2, text clips only: built-in modal editor (distinct from Ctrl+E/_edit_current,
        which shells out to an external editor and is unrelated/unchanged)."""
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        if clip.kind not in EDITABLE_KINDS:
            return
        text = self.store.get_data(clip.id).get("text/plain", b"").decode("utf-8", errors="replace")
        dialog = EditDialog(text, self)
        if dialog.exec():
            self.store.update_content(clip.id, {"text/plain": dialog.text().encode("utf-8")})
            self.refresh()
        self.search_edit.setFocus()

    def _edit_current(self) -> None:
        row = self._current_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        if clip.kind not in EXTERNAL_EDIT_KINDS:
            return
        suffixes = {"text": ".txt", "html": ".html", "image": ".png"}
        mime_types = {"text": "text/plain", "html": "text/html", "image": "image/png"}
        mime_data = self.store.get_data(clip.id)
        source = mime_data.get(mime_types[clip.kind], b"")
        fd, path = tempfile.mkstemp(prefix="keeps-edit-", suffix=suffixes[clip.kind])
        if clip.kind == "text":
            text = source.decode("utf-8", errors="replace")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(source)
        self._edit_sessions[path] = (clip.id, clip.kind)
        self._edit_watcher.addPath(path)
        editor_key = f"general/external_editor_{clip.kind}"
        configured_editor = str(config.get(self._settings, editor_key)).strip()
        subprocess.Popen([configured_editor or "xdg-open", path])

    def _on_edited_file_changed(self, path: str) -> None:
        session = self._edit_sessions.get(path)
        if session is None or not os.path.exists(path):
            return
        clip_id, kind = session
        result_id = clip_id
        if kind == "text":
            with open(path, encoding="utf-8") as f:
                new_text = f.read()
            result_id = self.store.update_content(
                clip_id, {"text/plain": new_text.encode("utf-8")}
            )
        else:
            with open(path, "rb") as f:
                new_bytes = f.read()
            if kind == "image":
                self._delegate.invalidate_thumbnail(clip_id)
                result_id = self.store.update_content(clip_id, {"image/png": new_bytes})
            else:
                mime_data = self.store.get_data(clip_id).copy()
                # Preserve the plain-text fallback; deriving it from edited
                # HTML is out of scope.
                mime_data["text/html"] = new_bytes
                result_id = self.store.update_content(clip_id, mime_data)
        if kind == "image":
            self.thumbnail_requested.emit(result_id, kind)
        self.refresh()
        if path not in self._edit_watcher.files():
            self._edit_watcher.addPath(path)  # some editors atomic-save (rm+recreate)
