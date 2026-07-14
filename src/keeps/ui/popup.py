"""Popup window: search-as-you-type list of clips, keymap per PLAN.md §6."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QCoreApplication,
    QEvent,
    QFileSystemWatcher,
    QModelIndex,
    QObject,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from keeps import clip_archive, compare, config, desktop_apps, multi_paste, paste
from keeps.ai import ranking
from keeps.ai.runtime import AiRuntime
from keeps.clipboard import make_mime_data
from keeps.hotkey.buffers import CopyBufferHotkeyManager
from keeps.hotkey.clip_registry import MAX_GLOBAL_CLIP_HOTKEYS
from keeps.hotkey.clips import ClipGlobalHotkeyManager
from keeps.popup_keymap import (
    DEFAULT_KEY_ALIASES,
    DEFAULT_POPUP_KEYBINDINGS,
    active_sequences,
    setting_key,
)
from keeps.search import MatchReason, remember_query
from keeps.store import Clip, Store
from keeps.ui import geometry, text_transform
from keeps.ui.delegate import ClipItemDelegate
from keeps.ui.expand_dialog import EditDialog, ViewDialog
from keeps.ui.properties_dialog import PropertiesDialog
from keeps.ui.qr_dialog import QrDialog
from keeps.ui.settings import SettingsDialog
from keeps.ui.workbench import WorkbenchDialog

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

_NAVIGATION_KEYS = {
    "navigate_up": Qt.Key.Key_Up,
    "navigate_down": Qt.Key.Key_Down,
    "navigate_page_up": Qt.Key.Key_PageUp,
    "navigate_page_down": Qt.Key.Key_PageDown,
    "navigate_home": Qt.Key.Key_Home,
    "navigate_end": Qt.Key.Key_End,
}

_LOCAL_HOTKEY_MODIFIERS = (
    Qt.KeyboardModifier.ControlModifier
    | Qt.KeyboardModifier.AltModifier
    | Qt.KeyboardModifier.MetaModifier
)


def _local_hotkey_error(sequence_text: str, reserved: set[str]) -> str | None:
    """Return why a local shortcut would interfere with normal popup input."""
    if sequence_text in reserved:
        return "reserved"
    sequence = QKeySequence(sequence_text)
    if sequence[0].keyboardModifiers() & _LOCAL_HOTKEY_MODIFIERS:
        return None
    return "modifier-required"

_MODE_BADGE_LABELS = {
    ranking.SearchMode.BLENDED: "auto",
    ranking.SearchMode.KEYWORD: "keywords",
    ranking.SearchMode.SEMANTIC: "meaning",
}

# Clicking the badge cycles modes the same way Ctrl+M does (PLAN.md §9);
# the tooltip exists because "auto"/"keywords"/"meaning" alone read as
# unexplained jargon otherwise (user feedback 2026-07-11).
_MODE_BADGE_TOOLTIPS = {
    ranking.SearchMode.BLENDED: (
        "Auto (default): exact matches first, semantically related results "
        "included below them. Click to switch mode, or Ctrl+M."
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
        self._semantic_scores: dict[int, float] = {}
        self._match_reasons: dict[int, MatchReason] = {}
        self._display_texts: dict[int, str] = {}
        # Ф9.3: ids of every clip *pasted* (not merely copied) this popup
        # session, all highlighted by the delegate simultaneously (user
        # request 2026-07-12: pasting several different clips in a row should
        # keep all of them marked, not just the most recent one). Session-
        # local, not persisted; survives _rebuild() since it lives outside
        # self._clips and is looked up by id, never by row index.
        self._pasted_ids: set[int] = set()
        self._scope = "history"

    @property
    def pasted_ids(self) -> frozenset[int]:
        return frozenset(self._pasted_ids)

    def mark_pasted(self, clip_id: int) -> None:
        self._pasted_ids.add(clip_id)

    @property
    def current_query(self) -> str:
        return self._current_query

    def match_reason(self, clip_id: int) -> MatchReason | None:
        return self._match_reasons.get(clip_id)

    def display_text(self, clip: Clip) -> str:
        return self._display_texts.get(clip.id, clip.alias or clip.preview)

    def set_query(self, query: str) -> None:
        self._current_query = query
        self._semantic_scores = {}
        self._rebuild()
        if self._ai_runtime is not None and self._ai_runtime.rag_text_enabled and query.strip():
            self._ai_runtime.encode_query_async(query, self._on_semantic_scores)

    def set_scope(self, scope: str) -> None:
        self._scope = scope
        self._rebuild()

    def _on_semantic_scores(self, query: str, scores: dict[int, float]) -> None:
        if query != self._current_query:
            return  # a newer keystroke already superseded this search
        self._semantic_scores = scores
        self._rebuild()

    def _rebuild(self) -> None:
        substring_clips, keyword_reasons = self._store.search_with_reasons(
            self._current_query
        )
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
        clips = self._store.clips_in_scope(self._scope, clips)

        ai_badges_active = self._ai_runtime is not None and (
            self._ai_runtime.rag_text_enabled or self._ai_runtime.ocr_enabled
        )
        semantic_only = (
            rag_active
            and self._ai_runtime.search_mode == ranking.SearchMode.SEMANTIC
        )
        display_texts = {}
        if not semantic_only:
            for clip in clips:
                keyword_reason = keyword_reasons.get(clip.id)
                if keyword_reason is None:
                    continue
                snippet = self._store.search_snippet(
                    clip.id, self._current_query, keyword_reason
                )
                if snippet:
                    display_texts[clip.id] = snippet

        match_reasons: dict[int, MatchReason] = {}
        if ai_badges_active:
            for clip in clips:
                keyword_reason = keyword_reasons.get(clip.id)
                if not semantic_only and keyword_reason is not None:
                    match_reasons[clip.id] = keyword_reason
                elif rag_active:
                    match_reasons[clip.id] = MatchReason.SEMANTIC
        self.beginResetModel()
        self._clips = clips
        self._match_reasons = match_reasons
        self._display_texts = display_texts
        self.endResetModel()

    def clip_at(self, row: int) -> Clip:
        return self._clips[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._clips)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return self.display_text(self._clips[index.row()])
        return None


class _PasteInjectionTask(QRunnable):
    """Runs ydotool/xdotool off the main thread.

    The main thread owns the clipboard right after _set_clipboard, so it must
    stay responsive to serve paste requests from other apps -- a subprocess
    hang here (ydotoold under load, PLAN.md §11) must never block it.
    paste.inject_paste() also carries its own timeout as defense in depth.
    """

    def __init__(
        self, backend: str, shortcut: str, completion: _PasteCompletion | None = None
    ) -> None:
        super().__init__()
        self._backend = backend
        self._shortcut = shortcut
        self._completion = completion

    def run(self) -> None:
        if not paste.inject_paste(
            self._backend, shutil.which, subprocess.run, self._shortcut
        ):
            paste.notify_paste_unavailable(self._backend, shutil.which)
        if self._completion is not None:
            self._completion.finished.emit()


class _PasteCompletion(QObject):
    finished = Signal()


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
        self,
        store: Store,
        ai_runtime: AiRuntime | None = None,
        parent: QWidget | None = None,
        *,
        clip_hotkeys: ClipGlobalHotkeyManager | None = None,
        buffer_hotkeys: CopyBufferHotkeyManager | None = None,
        settings_applier: Callable[[], None] | None = None,
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
        self._settings = config.open_settings()
        self._search_history = self._load_search_history()
        self._preserve_search_once = False
        self._target_app_class: str | None = None
        self._persistent = False
        self._restore_persistent_after_paste = False
        self._clip_hotkeys = clip_hotkeys
        self._buffer_hotkeys = buffer_hotkeys
        self._settings_applier = settings_applier
        self._local_hotkeys: dict[int, QShortcut] = {}
        self._forwarding_navigation = False
        self.model = ClipListModel(store, ai_runtime)

        self.search_edit = QLineEdit(self)
        self.search_edit.setPlaceholderText(self.tr("Search clips..."))
        self.search_edit.installEventFilter(self)

        # Search-mode badge (Ctrl+M cycles auto/keywords/meaning) -- only
        # shown when ai/rag_text_enabled, since there's nothing to switch
        # between otherwise (PLAN.md §9).
        self._mode_badge = QLabel(self)
        self._mode_badge.setVisible(False)
        self._mode_badge.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mode_badge.installEventFilter(self)

        self._history_menu = QMenu(self)
        self._history_button = QToolButton(self)
        self._history_button.setText(self.tr("History"))
        self._history_button.setToolTip(self.tr("Recent searches"))
        self._history_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._history_button.setMenu(self._history_menu)
        self._refresh_history_menu()

        self.list_view = QListView(self)
        self.list_view.setModel(self.model)
        self._delegate = ClipItemDelegate(store, self.list_view)
        self.list_view.setItemDelegate(self._delegate)
        self.list_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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

        self.tabs = QTabBar(self)
        self.tabs.setExpanding(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tabs.customContextMenuRequested.connect(self._show_tab_context_menu)
        self.tabs.installEventFilter(self)
        self._refresh_tabs()

        search_row = QHBoxLayout()
        search_row.addWidget(self.search_edit)
        search_row.addWidget(self._history_button)
        search_row.addWidget(self._mode_badge)

        self._title_bar = _TitleBar(self)
        title_bar_layout = QHBoxLayout(self._title_bar)
        title_bar_layout.setContentsMargins(2, 0, 2, 0)
        self._count_label = QLabel(self._title_bar)
        title_bar_layout.addWidget(self._count_label)
        self.model.modelReset.connect(self._update_count_label)
        title_bar_layout.addStretch(1)
        settings_button = QToolButton(self._title_bar)
        settings_button.setIcon(QIcon.fromTheme("configure"))
        settings_button.setToolTip(self.tr("Settings..."))
        settings_button.setAutoRaise(True)
        settings_button.clicked.connect(self._open_settings)
        title_bar_layout.addWidget(settings_button)
        new_clip_button = QToolButton(self._title_bar)
        new_clip_button.setIcon(QIcon.fromTheme("document-new"))
        new_clip_button.setToolTip(self.tr("New clip"))
        new_clip_button.setAutoRaise(True)
        new_clip_button.clicked.connect(self.new_clip)
        title_bar_layout.addWidget(new_clip_button)
        import_button = QToolButton(self._title_bar)
        import_button.setIcon(QIcon.fromTheme("document-import"))
        import_button.setToolTip(self.tr("Import clips..."))
        import_button.setAutoRaise(True)
        import_button.clicked.connect(self._import_clips)
        title_bar_layout.addWidget(import_button)
        workbench_button = QToolButton(self._title_bar)
        workbench_button.setIcon(QIcon.fromTheme("view-list-details"))
        workbench_button.setToolTip(self.tr("Clipboard Workbench"))
        workbench_button.setAutoRaise(True)
        workbench_button.clicked.connect(self._open_workbench)
        title_bar_layout.addWidget(workbench_button)
        self._persistent_button = QToolButton(self._title_bar)
        self._persistent_button.setIcon(QIcon.fromTheme("pin"))
        self._persistent_button.setToolTip(self.tr("Keep popup open"))
        self._persistent_button.setCheckable(True)
        self._persistent_button.setAutoRaise(True)
        self._persistent_button.toggled.connect(self._set_persistent)
        title_bar_layout.addWidget(self._persistent_button)

        layout = QVBoxLayout(self)
        # Matches _RESIZE_MARGIN so the drag-resize hit-test ring is never
        # covered by a child widget (search_edit/list_view).
        layout.setContentsMargins(*([_RESIZE_MARGIN] * 4))
        layout.addWidget(self._title_bar)
        layout.addLayout(search_row)
        layout.addWidget(self.tabs)
        layout.addWidget(self.list_view)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._apply_filter)
        self.search_edit.textChanged.connect(lambda _text: self._debounce.start())

        self._edit_watcher = QFileSystemWatcher(self)
        self._edit_watcher.fileChanged.connect(self._on_edited_file_changed)
        self._edit_sessions: dict[str, tuple[int, str]] = {}

        size = self._settings.value("popup/size")
        self.resize(size if size is not None else DEFAULT_SIZE)

        # Ctrl+scroll / Ctrl+Plus / Ctrl+Minus (§6): scales the whole popup UI
        # (font + delegate thumbnail/padding), not just text, and is
        # remembered across sessions the same way popup/size is.
        self._base_point_size = self.font().pointSizeF()
        self._ui_scale = float(self._settings.value("popup/ui_scale", 1.0))
        self._apply_ui_scale()

        self.paste_requested.connect(self._schedule_paste_injection)
        self._refresh_local_hotkeys()

    # -- lifecycle ---------------------------------------------------------

    def show_popup(self) -> None:
        backend = paste.session_backend()
        self._target_app_class = paste.active_app_class(
            backend, shutil.which, subprocess.run
        )
        if not self._preserve_search_once:
            self.search_edit.clear()
        self._preserve_search_once = False
        self._update_mode_badge()
        self._refresh_tabs()
        self.refresh()
        self._select_row(0)
        self._drop_stale_surface()
        self.show()
        self.raise_()
        self.activateWindow()
        self.search_edit.setFocus()

    def set_clip_hotkey_manager(self, manager: ClipGlobalHotkeyManager) -> None:
        """Attach the daemon-owned global action registry after popup creation."""
        self._clip_hotkeys = manager

    def set_buffer_hotkey_manager(self, manager: CopyBufferHotkeyManager) -> None:
        """Let the popup's Settings path apply buffer bindings immediately."""
        self._buffer_hotkeys = manager

    def set_settings_applier(self, callback: Callable[[], None]) -> None:
        """Attach the daemon-owned Apply action after its runtime objects exist."""
        self._settings_applier = callback

    def apply_settings(self) -> None:
        """Refresh this window's QSettings view and live local bindings."""
        self._settings.sync()
        self._refresh_local_hotkeys()
        self.refresh()

    def _hotkey_error_text(self, error: str) -> str:
        messages = {
            "no-session-dbus": self.tr("No session D-Bus connection."),
            "kglobalaccel-unavailable": self.tr(
                "Global hotkeys require KDE Plasma's KGlobalAccel service."
            ),
            "invalid": self.tr("The shortcut is invalid."),
            "availability-check-failed": self.tr(
                "Could not check whether this global shortcut is available."
            ),
            "conflict": self.tr("This global shortcut is already in use."),
            "registration-failed": self.tr("KGlobalAccel could not register this shortcut."),
            "component-failed": self.tr("KGlobalAccel could not create the shortcut action."),
            "signal-connection-failed": self.tr(
                "KGlobalAccel could not receive shortcut events."
            ),
            "limit": self.tr(
                "At most {limit} clip global hotkeys are supported."
            ).format(limit=MAX_GLOBAL_CLIP_HOTKEYS),
        }
        return messages.get(error, self.tr("Could not register this global shortcut."))

    def paste_clip_from_global_hotkey(self, clip_id: int) -> None:
        """Paste directly into the currently active app without showing the popup."""
        if not any(clip.id == clip_id for clip in self.store.all()):
            if self._clip_hotkeys is not None:
                self._clip_hotkeys.unregister(clip_id)
            return
        backend = paste.session_backend()
        self._target_app_class = paste.active_app_class(
            backend, shutil.which, subprocess.run
        )
        self._activate_id(
            clip_id, plain_only=False, want_paste=True, allow_persistent=False
        )

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
        self._remember_search_query(self.search_edit.text())
        self._settings.setValue("popup/size", self.size())
        super().hideEvent(event)

    def _load_search_history(self) -> list[str]:
        raw = self._settings.value("popup/search_history", [])
        if isinstance(raw, str):
            return [raw] if raw else []
        return [str(item) for item in raw]

    def _remember_search_query(self, query: str) -> None:
        updated = remember_query(self._search_history, query)
        if updated == self._search_history:
            return
        self._search_history = updated
        self._settings.setValue("popup/search_history", updated)
        self._refresh_history_menu()

    def _refresh_history_menu(self) -> None:
        self._history_menu.clear()
        for query in self._search_history:
            self._history_menu.addAction(query, lambda q=query: self.search_edit.setText(q))
        self._history_button.setEnabled(bool(self._search_history))

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate:
            # Opening one of our own modal dialogs (F2/F3/Settings) also
            # deactivates the popup; hiding it then strands the dialog with
            # no parent window under it (possibly on another screen) and the
            # popup looks "hung" behind the invisible modal grab.
            if not self._persistent and QGuiApplication.modalWindow() is None:
                self.hide()
        return super().event(event)

    def _set_persistent(self, enabled: bool) -> None:
        """Keep this session's popup visible across focus loss and paste."""
        self._persistent = enabled
        self._persistent_button.setToolTip(
            self.tr("Stop keeping popup open")
            if enabled
            else self.tr("Keep popup open")
        )

    def refresh(self) -> None:
        self._prune_deleted_global_hotkeys()
        self.model.set_query(self.search_edit.text())
        self._update_count_label()
        self._prune_thumbnail_cache()
        self._refresh_local_hotkeys()

    def _update_count_label(self) -> None:
        scope = str(self.tabs.tabData(self.tabs.currentIndex()))
        total = len(self.store.clips_in_scope(scope))
        self._count_label.setText(self.tr("{shown} shown / {total} total").format(
            shown=self.model.rowCount(), total=total
        ))

    def _refresh_tabs(self) -> None:
        current_scope = (
            self.tabs.tabData(self.tabs.currentIndex()) if self.tabs.count() else "history"
        )
        self.tabs.blockSignals(True)
        while self.tabs.count():
            self.tabs.removeTab(0)
        self.tabs.addTab(self.tr("History"))
        self.tabs.setTabData(0, "history")
        self.tabs.addTab(self.tr("Pinned"))
        self.tabs.setTabData(1, "pinned")
        for group in self.store.groups():
            index = self.tabs.addTab(group.name)
            self.tabs.setTabData(index, f"group:{group.id}")
        target = next(
            (i for i in range(self.tabs.count()) if self.tabs.tabData(i) == current_scope),
            0,
        )
        self.tabs.setCurrentIndex(target)
        self.tabs.blockSignals(False)
        self.model.set_scope(str(self.tabs.tabData(target)))

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        self.model.set_scope(str(self.tabs.tabData(index)))
        self._update_count_label()
        self._select_row(0)

    def on_clip_captured(self, _clip_id: int, _kind: str) -> None:
        """Drop cached pixmaps for clips removed by Store.trim() during capture."""
        self._prune_thumbnail_cache()
        self._prune_deleted_global_hotkeys()
        self._refresh_local_hotkeys()

    def _refresh_local_hotkeys(self) -> None:
        """Rebuild the popup-only shortcut table from durable clip assignments."""
        assignments = {
            clip.id: clip.hotkey
            for clip in self.store.clips_with_hotkeys()
            if clip.hotkey and not clip.hotkey_global
        }
        for clip_id in set(self._local_hotkeys) - set(assignments):
            shortcut = self._local_hotkeys.pop(clip_id)
            shortcut.setEnabled(False)
            shortcut.deleteLater()
        for clip_id, sequence in assignments.items():
            shortcut = self._local_hotkeys.get(clip_id)
            if shortcut is not None and shortcut.key().toString(
                QKeySequence.SequenceFormat.PortableText
            ) == sequence:
                continue
            if shortcut is not None:
                shortcut.setEnabled(False)
                shortcut.deleteLater()
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(
                lambda clip_id=clip_id: self._activate_local_hotkey(clip_id)
            )
            self._local_hotkeys[clip_id] = shortcut

    def _activate_local_hotkey(self, clip_id: int) -> None:
        if self.isVisible():
            self._activate_id(clip_id, plain_only=False, want_paste=True)

    def _prune_deleted_global_hotkeys(self) -> None:
        if self._clip_hotkeys is not None:
            self._clip_hotkeys.prune({clip.id for clip in self.store.all()})

    def _update_clip_content(self, clip_id: int, mime_data: dict[str, bytes]) -> int:
        """Edit a clip and immediately release its action if dedup deletes it."""
        result_id = self.store.update_content(clip_id, mime_data)
        if result_id != clip_id and self._clip_hotkeys is not None:
            self._clip_hotkeys.unregister(clip_id)
        return result_id

    def _prune_thumbnail_cache(self) -> None:
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

    def _selected_rows(self) -> list[int]:
        selection_model = self.list_view.selectionModel()
        return sorted(index.row() for index in selection_model.selectedRows())

    def _single_selected_row(self) -> int | None:
        rows = self._selected_rows()
        return rows[0] if len(rows) == 1 else None

    # -- keymap (PLAN.md §6), paste injection itself is Ф4 -----------------

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel and self._handle_wheel(event):
            return True
        if obj is self.search_edit and event.type() == QEvent.Type.KeyPress:
            return self._handle_key(event)
        if obj is self.list_view and event.type() == QEvent.Type.KeyPress:
            if event.key() in set(_NAVIGATION_KEYS.values()):
                if self._forwarding_navigation:
                    return False
                # Swallow an old default after a rebind; the configured action
                # below forwards only its selected navigation command.
                return self._handle_key(event) or True
            return self._handle_key(event)
        if obj is self.tabs and event.type() == QEvent.Type.KeyPress:
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

    def _keybinding_text(self, action: str) -> str:
        return str(config.get(self._settings, setting_key(action)))

    def _matches_keybinding(self, event: QKeyEvent, action: str) -> bool:
        sequence_text = self._keybinding_text(action)
        sequence = QKeySequence(sequence_text)
        if sequence.count() == 1 and event.keyCombination() == sequence[0]:
            return True
        if sequence_text != DEFAULT_POPUP_KEYBINDINGS[action]:
            return False
        return any(
            event.keyCombination() == QKeySequence(alias)[0]
            for alias in DEFAULT_KEY_ALIASES.get(action, ())
        )

    def _reserved_popup_hotkeys(self) -> set[str]:
        reserved = {
            QKeySequence(self._keybinding_text(action)).toString(
                QKeySequence.SequenceFormat.PortableText
            )
            for action in DEFAULT_POPUP_KEYBINDINGS
        }
        for action in DEFAULT_KEY_ALIASES:
            reserved.update(active_sequences(action, self._keybinding_text(action)))
        return reserved

    def _handle_key(self, event: QKeyEvent) -> bool:
        for action, navigation_key in _NAVIGATION_KEYS.items():
            if self._matches_keybinding(event, action):
                self._forwarding_navigation = True
                try:
                    QCoreApplication.sendEvent(
                        self.list_view,
                        QKeyEvent(
                            QEvent.Type.KeyPress,
                            navigation_key,
                            Qt.KeyboardModifier.NoModifier,
                        ),
                    )
                finally:
                    self._forwarding_navigation = False
                return True
        if self._matches_keybinding(event, "next_tab"):
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count())
            return True
        if self._matches_keybinding(event, "previous_tab"):
            self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % self.tabs.count())
            return True
        if self._matches_keybinding(event, "select_all"):
            self.list_view.selectAll()
            return True
        if self._matches_keybinding(event, "hide"):
            self.hide()
            return True
        if self._matches_keybinding(event, "properties"):
            self._properties_current()
            return True
        if self._matches_keybinding(event, "paste"):
            rows = self._selected_rows()
            if len(rows) > 1:
                self._activate_many(rows, want_paste=True)
            elif rows:
                self._activate(rows[0], plain_only=False, want_paste=True)
            return True
        if self._matches_keybinding(event, "paste_plain"):
            rows = self._selected_rows()
            if len(rows) > 1:
                self._activate_many(rows, want_paste=True)
            elif rows:
                self._activate(rows[0], plain_only=True, want_paste=True)
            return True
        if self._matches_keybinding(event, "copy"):
            rows = self._selected_rows()
            if len(rows) > 1:
                self._activate_many(rows, want_paste=False)
            elif rows:
                self._activate(rows[0], plain_only=False, want_paste=False)
            return True
        if self._matches_keybinding(event, "delete"):
            self._delete_current()
            return True
        if self._matches_keybinding(event, "edit_external"):
            self._edit_current()
            return True
        if self._matches_keybinding(event, "view"):
            self._view_current()
            return True
        if self._matches_keybinding(event, "edit"):
            self._edit_builtin_current()
            return True
        if self._matches_keybinding(event, "pin"):
            self._toggle_pin_current()
            return True
        if self._matches_keybinding(event, "search_mode"):
            self._cycle_search_mode()
            return True
        if self._matches_keybinding(event, "scale_up"):
            self._set_ui_scale(geometry.next_ui_scale(self._ui_scale, 1))
            return True
        if self._matches_keybinding(event, "scale_down"):
            self._set_ui_scale(geometry.next_ui_scale(self._ui_scale, -1))
            return True
        for number in range(1, 10):
            if self._matches_keybinding(event, f"paste_{number}"):
                row = number - 1
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
        selected_rows = self._selected_rows()
        if row not in selected_rows:
            self.list_view.clearSelection()
            self._select_row(row)
            selected_rows = [row]
        clips = [self.model.clip_at(selected_row) for selected_row in selected_rows]
        selected_ids = [selected_clip.id for selected_clip in clips]
        clip = self.model.clip_at(row)
        multi = len(selected_rows) > 1

        menu = QMenu(self.list_view)

        def add_keyed_action(label: str, key_action: str, callback) -> QAction:
            action = menu.addAction(self.tr(label), callback)
            action.setShortcut(QKeySequence(self._keybinding_text(key_action)))
            action.setShortcutVisibleInContextMenu(True)
            return action

        add_keyed_action(
            "Paste",
            "paste",
            lambda: self._activate_selection_ids(
                selected_ids, plain_only=False, want_paste=True
            ),
        )
        add_keyed_action(
            "Paste as text",
            "paste_plain",
            lambda: self._activate_selection_ids(
                selected_ids, plain_only=True, want_paste=True
            ),
        )
        add_keyed_action(
            "Copy",
            "copy",
            lambda: self._activate_selection_ids(
                selected_ids, plain_only=False, want_paste=False
            ),
        )
        if not multi and clip.kind == "image" and clip.ocr_text and clip.ocr_text.strip():
            menu.addAction(self.tr("Copy recognized text"), lambda: self._copy_ocr_text(row))
        menu.addSeparator()
        view_action = add_keyed_action("View", "view", self._view_current)
        view_action.setEnabled(not multi)
        pin_target = not all(selected_clip.pinned for selected_clip in clips)
        pin_label = self.tr("Pin") if pin_target else self.tr("Unpin")
        add_keyed_action(
            pin_label,
            "pin",
            lambda: self._set_pinned_ids(selected_ids, pin_target),
        )
        groups = self.store.groups()
        if groups:
            group_menu = menu.addMenu(self.tr("Add to group"))
            group_menu.addAction(
                self.tr("No group"), lambda: self._set_group_ids(selected_ids, None)
            )
            for group in groups:
                group_menu.addAction(
                    group.name,
                    lambda _checked=False, group_id=group.id: self._set_group_ids(
                        selected_ids, group_id
                    ),
                )
        scope = str(self.tabs.tabData(self.tabs.currentIndex()))
        if scope != "history":
            move_menu = menu.addMenu(self.tr("Move"))
            move_menu.addAction(
                self.tr("Up"), lambda: self._move_manual_ids(selected_ids, scope, -1)
            )
            move_menu.addAction(
                self.tr("Down"), lambda: self._move_manual_ids(selected_ids, scope, 1)
            )
        builtin_edit_action = add_keyed_action("Edit", "edit", self._edit_builtin_current)
        builtin_edit_action.setEnabled(not multi and clip.kind in EDITABLE_KINDS)
        edit_action = add_keyed_action("Edit externally", "edit_external", self._edit_current)
        edit_action.setEnabled(not multi and clip.kind in EXTERNAL_EDIT_KINDS)
        properties_action = add_keyed_action(
            "Properties", "properties", lambda: self._properties_id(clip.id)
        )
        properties_action.setEnabled(not multi)
        plain_text = self.store.get_data(clip.id).get("text/plain") if not multi else None
        qr_action = menu.addAction(
            self.tr("View as QR"), lambda: self._view_qr_id(clip.id)
        )
        qr_action.setEnabled(not multi and plain_text is not None)
        add_keyed_action("Delete", "delete", lambda: self._delete_ids(selected_ids))

        menu.addSeparator()
        menu.addAction(
            self.tr("Open Clipboard Workbench..."),
            lambda: self._open_workbench(selected_ids),
        )
        compare_action = menu.addAction(
            self.tr("Compare"), lambda: self._compare_ids(selected_ids)
        )
        comparison_available = len(selected_ids) == 2 and compare.comparison_payload(
            self.store.get_data(selected_ids[0]), self.store.get_data(selected_ids[1])
        ) is not None
        compare_action.setEnabled(comparison_available)
        menu.addAction(self.tr("Export selected..."), lambda: self._export_ids(selected_ids))
        menu.addAction(
            self.tr("Export as Keeps archive..."),
            lambda: self._export_archive_ids(selected_ids),
        )

        if plain_text is not None:
            menu.addSeparator()
            special_menu = menu.addMenu(self.tr("Special Paste"))
            for label, transform in text_transform.TRANSFORMS.items():
                action = special_menu.addAction(
                    self.tr(label),
                    lambda _checked=False, t=transform: self._special_paste(clip.id, t),
                )
                if label == "JSON pretty-print":
                    action.setEnabled(
                        text_transform.is_valid_json(
                            plain_text.decode("utf-8", errors="replace")
                        )
                    )

        menu.exec(self.list_view.viewport().mapToGlobal(pos))

    def _show_tab_context_menu(self, pos) -> None:
        index = self.tabs.tabAt(pos)
        menu = QMenu(self.tabs)
        menu.addAction(self.tr("New group"), self._create_group)
        if index >= 2:
            group_id = int(str(self.tabs.tabData(index)).partition(":")[2])
            menu.addAction(
                self.tr("Rename group"), lambda: self._rename_group(group_id)
            )
            menu.addAction(
                self.tr("Delete group"), lambda: self._delete_group(group_id)
            )
        menu.exec(self.tabs.mapToGlobal(pos))

    def _create_group(self) -> None:
        name, accepted = QInputDialog.getText(self, self.tr("New group"), self.tr("Name"))
        if not accepted or not name.strip():
            return
        try:
            group_id = self.store.create_group(name)
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Cannot create group"), str(exc))
            return
        self._refresh_tabs()
        self._select_scope(f"group:{group_id}")

    def _rename_group(self, group_id: int) -> None:
        group = next(group for group in self.store.groups() if group.id == group_id)
        name, accepted = QInputDialog.getText(
            self, self.tr("Rename group"), self.tr("Name"), text=group.name
        )
        if not accepted or not name.strip():
            return
        try:
            self.store.rename_group(group_id, name)
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Cannot rename group"), str(exc))
            return
        self._refresh_tabs()

    def _delete_group(self, group_id: int) -> None:
        answer = QMessageBox.question(
            self,
            self.tr("Delete group"),
            self.tr("Delete this group? Its clips will remain in History."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_group(group_id)
        self._refresh_tabs()
        self.refresh()

    def _select_scope(self, scope: str) -> None:
        for index in range(self.tabs.count()):
            if self.tabs.tabData(index) == scope:
                self.tabs.setCurrentIndex(index)
                return

    def _copy_ocr_text(self, row: int) -> None:
        clip = self.model.clip_at(row)
        self._set_clipboard({"text/plain": clip.ocr_text.encode("utf-8")}, plain_only=True)
        self.store.touch(clip.id)
        if not self._persistent:
            self.hide()

    def _special_paste(self, clip_id: int, transform) -> None:
        """Paste a transformed copy of a clip's plain text without touching storage.

        Ditto-style Special Paste: the transform only affects what lands on
        the clipboard/target app, never the stored clip (no update_content
        call), same as the source clip content shown in the list afterwards.
        """
        plain = (
            self.store.get_data(clip_id)
            .get("text/plain", b"")
            .decode("utf-8", errors="replace")
        )
        transformed = transform(plain)
        self._set_clipboard({"text/plain": transformed.encode("utf-8")}, plain_only=True)
        self.store.touch(clip_id)
        self.model.mark_pasted(clip_id)
        self._preserve_search_once = bool(
            config.get(self._settings, "popup/keep_search_after_paste")
        )
        self._hide_before_paste(restore_persistent=self._persistent)
        self.paste_requested.emit(clip_id, True)

    def new_clip(self) -> None:
        """Create a text clip from the same editor used by F2."""
        dialog = EditDialog("", self)
        if not dialog.exec():
            self.search_edit.setFocus()
            return
        clip_id = self.store.add("text", {"text/plain": dialog.text().encode("utf-8")})
        self._after_local_add(clip_id, "text")

    def _export_ids(self, clip_ids: list[int]) -> None:
        if not clip_ids:
            return
        if len(clip_ids) == 1:
            clip = next((item for item in self.store.all() if item.id == clip_ids[0]), None)
            if clip is None:
                return
            try:
                suffix, data = clip_archive.content_export(clip.kind, self.store.get_data(clip.id))
            except ValueError as exc:
                QMessageBox.warning(self, self.tr("Cannot export clip"), str(exc))
                return
            filters = {
                ".png": self.tr("PNG image (*.png)"),
                ".html": self.tr("HTML document (*.html)"),
                ".txt": self.tr("Text file (*.txt)"),
            }
            filename, _selected_filter = QFileDialog.getSaveFileName(
                self,
                self.tr("Export clip content"),
                f"keeps-clip{suffix}",
                filters[suffix],
            )
            if not filename:
                return
            path = Path(filename)
            if path.suffix.casefold() != suffix:
                path = path.with_name(f"{path.name}{suffix}")
            try:
                path.write_bytes(data)
            except OSError as exc:
                QMessageBox.warning(self, self.tr("Cannot export clip"), str(exc))
            self.search_edit.setFocus()
            return
        self._export_archive_ids(clip_ids)

    def _export_archive_ids(self, clip_ids: list[int]) -> None:
        if not clip_ids:
            return
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            self.tr("Export clips"),
            "keeps-clips.keeps.json",
            self.tr("Keeps clips (*.keeps.json)"),
        )
        if not filename:
            return
        path = Path(filename)
        if not path.name.endswith(".keeps.json"):
            path = path.with_name(f"{path.name}.keeps.json")
        clips_by_id = {clip.id: clip for clip in self.store.all()}
        records = [
            clip_archive.ArchiveClip(
                clips_by_id[clip_id].kind,
                self.store.get_data(clip_id),
                pinned=clips_by_id[clip_id].pinned,
                alias=clips_by_id[clip_id].alias,
            )
            for clip_id in clip_ids
            if clip_id in clips_by_id
        ]
        try:
            path.write_bytes(clip_archive.encode_archive(records))
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Cannot export clips"), str(exc))
        self.search_edit.setFocus()

    def _open_workbench(self, clip_ids: list[int] | None = None) -> None:
        if clip_ids is None:
            rows = self._selected_rows()
            clip_ids = [self.model.clip_at(row).id for row in rows]
        if not clip_ids:
            QMessageBox.information(
                self,
                self.tr("Clipboard Workbench"),
                self.tr("Select one or more clips first."),
            )
            return
        dialog = WorkbenchDialog(
            self.store,
            clip_ids,
            str(config.get(self._settings, "paste/multi_separator")),
            self,
        )
        if dialog.exec() != WorkbenchDialog.DialogCode.Accepted:
            self.search_edit.setFocus()
            return
        result = dialog.result()
        if result is None or dialog.action is None:
            self.search_edit.setFocus()
            return
        if result.skipped_ids:
            QMessageBox.warning(
                self,
                self.tr("Some clips were skipped"),
                self.tr("{count} clip(s) without plain text were skipped.").format(
                    count=len(result.skipped_ids)
                ),
            )
        if dialog.action == "save":
            clip_id = self.store.add(result.kind, result.mime_data)
            self._after_local_add(clip_id, result.kind)
            return

        self._set_clipboard(result.mime_data, plain_only=result.plain_only)
        self.store.touch_many(list(result.included_ids))
        for clip_id in result.included_ids:
            self.model.mark_pasted(clip_id)
        self._preserve_search_once = bool(
            config.get(self._settings, "popup/keep_search_after_paste")
        )
        self._hide_before_paste(restore_persistent=self._persistent)
        self.paste_requested.emit(result.included_ids[0], result.plain_only)

    def _import_clips(self) -> None:
        filename, _selected_filter = QFileDialog.getOpenFileName(
            self,
            self.tr("Import clips"),
            "",
            self.tr("Keeps clips (*.keeps.json);;All files (*)"),
        )
        if not filename:
            return
        try:
            records = clip_archive.decode_archive(Path(filename).read_bytes())
        except (OSError, ValueError) as exc:
            # tr() on the dynamic text: clip_archive's known error strings are
            # catalog keys; anything else (OSError) passes through unchanged.
            QMessageBox.warning(self, self.tr("Cannot import clips"), self.tr(str(exc)))
            self.search_edit.setFocus()
            return
        inserted = 0
        duplicates = 0
        for record in records:
            clip_id, was_inserted = self.store.import_clip(record)
            if was_inserted:
                inserted += 1
                self._after_local_add(clip_id, record.kind, refresh=False)
            else:
                duplicates += 1
        self.refresh()
        QMessageBox.information(
            self,
            self.tr("Import complete"),
            self.tr("Imported {inserted} clip(s); skipped {duplicates} duplicate(s).").format(
                inserted=inserted, duplicates=duplicates
            ),
        )
        self.search_edit.setFocus()

    def _compare_ids(self, clip_ids: list[int]) -> None:
        if len(clip_ids) != 2:
            return
        payload = compare.comparison_payload(
            self.store.get_data(clip_ids[0]), self.store.get_data(clip_ids[1])
        )
        if payload is None:
            QMessageBox.warning(
                self,
                self.tr("Cannot compare clips"),
                self.tr("The selected clips have no shared text format."),
            )
            return
        configured_diff = str(config.get(self._settings, "general/external_diff")).strip()
        command = compare.diff_command(configured_diff, shutil.which)
        if command is None:
            QMessageBox.warning(
                self,
                self.tr("No diff tool available"),
                self.tr("Install meld, kompare, or kdiff3, or configure an external diff tool."),
            )
            return
        suffix, left, right = payload
        directory = Path(tempfile.mkdtemp(prefix="keeps-compare-"))
        left_path, right_path = compare.write_comparison_pair(directory, suffix, left, right)
        try:
            argv = (
                desktop_apps.command_for_files(configured_diff, [left_path, right_path])
                if configured_diff
                else [*command, str(left_path), str(right_path)]
            )
            process = subprocess.Popen(argv)
        except OSError as exc:
            shutil.rmtree(directory, ignore_errors=True)
            QMessageBox.warning(self, self.tr("Cannot start diff tool"), str(exc))
            return
        threading.Thread(
            target=self._remove_compare_files_when_done,
            args=(process, directory),
            daemon=True,
        ).start()

    @staticmethod
    def _remove_compare_files_when_done(process: subprocess.Popen, directory: Path) -> None:
        process.wait()
        shutil.rmtree(directory, ignore_errors=True)

    def _after_local_add(self, clip_id: int, kind: str, *, refresh: bool = True) -> None:
        if self._ai_runtime is not None:
            self._ai_runtime.on_clip_captured(clip_id, kind)
        if kind == "image":
            self.thumbnail_requested.emit(clip_id, kind)
        self.on_clip_captured(clip_id, kind)
        if refresh:
            self.refresh()
            for row in range(self.model.rowCount()):
                if self.model.clip_at(row).id == clip_id:
                    self._select_row(row)
                    break
        self.search_edit.setFocus()

    # -- actions -------------------------------------------------------------

    def _activate_selection(
        self, rows: list[int], plain_only: bool, want_paste: bool
    ) -> None:
        clip_ids = [self.model.clip_at(row).id for row in rows]
        self._activate_selection_ids(clip_ids, plain_only, want_paste)

    def _activate_selection_ids(
        self, clip_ids: list[int], plain_only: bool, want_paste: bool
    ) -> None:
        if len(clip_ids) > 1:
            self._activate_ids(clip_ids, want_paste)
        elif clip_ids:
            self._activate_id(clip_ids[0], plain_only, want_paste)

    def _activate_many(self, rows: list[int], want_paste: bool) -> None:
        self._activate_ids([self.model.clip_at(row).id for row in rows], want_paste)

    def _activate_ids(self, clip_ids: list[int], want_paste: bool) -> None:
        selected = [(clip_id, self.store.get_data(clip_id)) for clip_id in clip_ids]
        result = multi_paste.combine_plain_text(
            selected,
            str(config.get(self._settings, "paste/multi_separator")),
            reverse=bool(config.get(self._settings, "paste/multi_reverse_order")),
        )
        if not result.clip_ids:
            QMessageBox.warning(
                self,
                self.tr("Nothing to paste"),
                self.tr("The selection contains no plain-text content."),
            )
            return
        if result.skipped_count:
            QMessageBox.warning(
                self,
                self.tr("Some clips were skipped"),
                self.tr("{count} clip(s) without plain text were skipped.").format(
                    count=result.skipped_count
                ),
            )
        combined_data = {"text/plain": result.text.encode("utf-8")}
        self._set_clipboard(combined_data, plain_only=True)
        self.store.touch_many(list(result.clip_ids))
        paste_now = want_paste
        if paste_now and config.get(self._settings, "paste/save_multi_as_clip"):
            combined_id = self.store.add("text", combined_data)
            if self._ai_runtime is not None:
                self._ai_runtime.on_clip_captured(combined_id, "text")
            self._prune_deleted_global_hotkeys()
        if paste_now:
            for clip_id in result.clip_ids:
                self.model.mark_pasted(clip_id)
            self._preserve_search_once = bool(
                config.get(self._settings, "popup/keep_search_after_paste")
            )
        if paste_now:
            self._hide_before_paste(restore_persistent=self._persistent)
        elif not self._persistent:
            self.hide()
        if paste_now:
            self.paste_requested.emit(result.clip_ids[0], True)

    def _activate(self, row: int, plain_only: bool, want_paste: bool) -> None:
        self._activate_id(self.model.clip_at(row).id, plain_only, want_paste)

    def _activate_id(
        self, clip_id: int, plain_only: bool, want_paste: bool, *, allow_persistent: bool = True
    ) -> None:
        mime_data = self.store.get_data(clip_id)
        self._set_clipboard(mime_data, plain_only)
        self.store.touch(clip_id)
        paste_now = want_paste
        if paste_now:
            self._preserve_search_once = bool(
                config.get(self._settings, "popup/keep_search_after_paste")
            )
        if paste_now:
            self._hide_before_paste(
                restore_persistent=self._persistent and allow_persistent
            )
        elif not self._persistent:
            self.hide()
        if paste_now:
            self.model.mark_pasted(clip_id)
            self.paste_requested.emit(clip_id, plain_only)

    @staticmethod
    def _set_clipboard(mime_data: dict[str, bytes], plain_only: bool) -> None:
        QGuiApplication.clipboard().setMimeData(make_mime_data(mime_data, plain_only=plain_only))

    def _hide_before_paste(self, *, restore_persistent: bool) -> None:
        """Return focus for injection, then restore a pinned popup after it completes."""
        self._restore_persistent_after_paste = restore_persistent
        self.hide()

    def _schedule_paste_injection(self, clip_id: int, plain_only: bool) -> None:
        # Plain-vs-rich is already decided by what's on the clipboard
        # (_set_clipboard above); injection just replays Ctrl+V.
        del clip_id, plain_only
        restore_popup = self._restore_persistent_after_paste
        self._restore_persistent_after_paste = False
        if not config.get(self._settings, "paste/enabled"):
            if restore_popup:
                self.show_popup()
            return
        delay_ms = int(config.get(self._settings, "paste/delay_ms"))
        backend = paste.session_backend()
        shortcut = paste.shortcut_for_app(
            self._target_app_class,
            str(config.get(self._settings, "paste/app_shortcuts")),
        )
        QTimer.singleShot(
            delay_ms,
            lambda: self._run_paste_injection(backend, shortcut, restore_popup),
        )

    def _run_paste_injection(self, backend: str, shortcut: str, restore_popup: bool) -> None:
        completion = _PasteCompletion(self) if restore_popup else None
        if completion is not None:
            completion.finished.connect(self.show_popup)
        QThreadPool.globalInstance().start(_PasteInjectionTask(backend, shortcut, completion))

    def _delete_current(self) -> None:
        rows = self._selected_rows()
        if not rows:
            return
        self._delete_rows(rows)

    def _delete_rows(self, rows: list[int]) -> None:
        self._delete_ids([self.model.clip_at(row).id for row in rows])

    def _delete_ids(self, clip_ids: list[int]) -> None:
        if len(clip_ids) > 5:
            answer = QMessageBox.question(
                self,
                self.tr("Delete clips"),
                self.tr("Delete {count} selected clips?").format(count=len(clip_ids)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        for clip_id in clip_ids:
            self._delegate.invalidate_thumbnail(clip_id)
            if self._clip_hotkeys is not None:
                self._clip_hotkeys.unregister(clip_id)
        self.store.delete_many(clip_ids)
        self.refresh()
        self._select_row(min(self._current_row() or 0, self.model.rowCount() - 1))

    def _open_settings(self) -> None:
        # Mirrors app.py's on_settings_requested (tray path) exactly, so
        # Settings behaves identically whether opened from the tray or here.
        SettingsDialog(
            self._ai_runtime,
            self.store,
            clip_hotkeys=self._clip_hotkeys,
            buffer_hotkeys=self._buffer_hotkeys,
            apply_callback=self._settings_applier,
        ).exec()
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
            self._mode_badge.setText(f"[{self.tr(_MODE_BADGE_LABELS[mode])}]")
            self._mode_badge.setToolTip(self.tr(_MODE_BADGE_TOOLTIPS[mode]))

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
        rows = self._selected_rows()
        if not rows:
            return
        clips = [self.model.clip_at(row) for row in rows]
        self._set_pinned_rows(rows, not all(clip.pinned for clip in clips))

    def _set_pinned_rows(self, rows: list[int], pinned: bool) -> None:
        self._set_pinned_ids([self.model.clip_at(row).id for row in rows], pinned)

    def _set_pinned_ids(self, clip_ids: list[int], pinned: bool) -> None:
        self.store.set_pinned_many(clip_ids, pinned)
        self.refresh()
        for row in range(self.model.rowCount()):
            if self.model.clip_at(row).id == clip_ids[0]:
                self._select_row(row)
                break

    def _set_group_ids(self, clip_ids: list[int], group_id: int | None) -> None:
        self.store.set_group_many(clip_ids, group_id)
        self.refresh()
        for row in range(self.model.rowCount()):
            if self.model.clip_at(row).id == clip_ids[0]:
                self._select_row(row)
                break

    def _move_manual_ids(self, clip_ids: list[int], scope: str, direction: int) -> None:
        ordered_ids = clip_ids if direction < 0 else list(reversed(clip_ids))
        for clip_id in ordered_ids:
            self.store.move_manual(clip_id, scope, direction)
        self.refresh()

    def _view_current(self) -> None:
        """F3, any kind: read-only expand (Ditto's "View Full Description")."""
        row = self._single_selected_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        mime_data = self.store.get_data(clip.id)
        ViewDialog(clip, mime_data, self).exec()
        self.search_edit.setFocus()

    def _properties_current(self) -> None:
        row = self._single_selected_row()
        if row is not None:
            self._properties_id(self.model.clip_at(row).id)

    def _properties_id(self, clip_id: int) -> None:
        clip = next((item for item in self.store.all() if item.id == clip_id), None)
        if clip is None:
            return
        dialog = PropertiesDialog(clip, self.store.mime_sizes(clip_id), self)
        if dialog.exec() != PropertiesDialog.DialogCode.Accepted:
            self.search_edit.setFocus()
            return
        hotkey = dialog.hotkey()
        hotkey_is_global = dialog.hotkey_is_global()
        local_error = (
            _local_hotkey_error(hotkey, self._reserved_popup_hotkeys())
            if hotkey and not hotkey_is_global
            else None
        )
        if local_error == "reserved":
            QMessageBox.warning(
                self,
                self.tr("Hotkey already assigned"),
                self.tr("This shortcut is reserved by the popup keymap."),
            )
            self.search_edit.setFocus()
            return
        if local_error == "modifier-required":
            QMessageBox.warning(
                self,
                self.tr("Hotkey needs a modifier"),
                self.tr("Local clip hotkeys must include Ctrl, Alt, or Meta."),
            )
            self.search_edit.setFocus()
            return
        if hotkey:
            conflict_id = self.store.hotkey_conflict(hotkey, exclude_clip_id=clip_id)
            if conflict_id is not None:
                QMessageBox.warning(
                    self,
                    self.tr("Hotkey already assigned"),
                    self.tr("This shortcut is already assigned to clip #{clip_id}.").format(
                        clip_id=conflict_id
                    ),
                )
                self.search_edit.setFocus()
                return
        if hotkey_is_global:
            if self._clip_hotkeys is None:
                QMessageBox.warning(
                    self,
                    self.tr("Global hotkey unavailable"),
                    self.tr("Global clip hotkeys are available only in the running daemon."),
                )
                self.search_edit.setFocus()
                return
            error = self._clip_hotkeys.register(clip_id, hotkey or "")
            if error:
                QMessageBox.warning(
                    self,
                    self.tr("Global hotkey unavailable"),
                    self._hotkey_error_text(error),
                )
                self.search_edit.setFocus()
                return
        elif clip.hotkey_global and self._clip_hotkeys is not None:
            self._clip_hotkeys.unregister(clip_id)
        self.store.set_alias(clip_id, dialog.alias())
        self.store.set_pinned(clip_id, dialog.pinned())
        self.store.set_hotkey(clip_id, hotkey, global_hotkey=hotkey_is_global)
        self.refresh()
        for row in range(self.model.rowCount()):
            if self.model.clip_at(row).id == clip_id:
                self._select_row(row)
                break
        self.search_edit.setFocus()

    def _view_qr_id(self, clip_id: int) -> None:
        plain = self.store.get_data(clip_id).get("text/plain")
        if plain is None:
            return
        try:
            QrDialog(plain.decode("utf-8", errors="replace"), self).exec()
        except Exception as exc:
            QMessageBox.warning(self, self.tr("Cannot create QR code"), str(exc))
        self.search_edit.setFocus()

    def _edit_builtin_current(self) -> None:
        """F2, text clips only: built-in modal editor (distinct from Ctrl+E/_edit_current,
        which shells out to an external editor and is unrelated/unchanged)."""
        row = self._single_selected_row()
        if row is None:
            return
        clip = self.model.clip_at(row)
        if clip.kind not in EDITABLE_KINDS:
            return
        text = self.store.get_data(clip.id).get("text/plain", b"").decode("utf-8", errors="replace")
        dialog = EditDialog(text, self)
        if dialog.exec():
            self._update_clip_content(
                clip.id, {"text/plain": dialog.text().encode("utf-8")}
            )
            self.refresh()
        self.search_edit.setFocus()

    def _edit_current(self) -> None:
        row = self._single_selected_row()
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
        subprocess.Popen(desktop_apps.command_for_files(configured_editor, [path]))

    def _on_edited_file_changed(self, path: str) -> None:
        session = self._edit_sessions.get(path)
        if session is None or not os.path.exists(path):
            return
        clip_id, kind = session
        result_id = clip_id
        if kind == "text":
            with open(path, encoding="utf-8") as f:
                new_text = f.read()
            result_id = self._update_clip_content(
                clip_id, {"text/plain": new_text.encode("utf-8")}
            )
        else:
            with open(path, "rb") as f:
                new_bytes = f.read()
            if kind == "image":
                self._delegate.invalidate_thumbnail(clip_id)
                result_id = self._update_clip_content(clip_id, {"image/png": new_bytes})
            else:
                mime_data = self.store.get_data(clip_id).copy()
                # Preserve the plain-text fallback; deriving it from edited
                # HTML is out of scope.
                mime_data["text/html"] = new_bytes
                result_id = self._update_clip_content(clip_id, mime_data)
        if kind == "image":
            self.thumbnail_requested.emit(result_id, kind)
        self.refresh()
        if path not in self._edit_watcher.files():
            self._edit_watcher.addPath(path)  # some editors atomic-save (rm+recreate)
