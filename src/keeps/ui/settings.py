"""Settings dialog, including the per-clip hotkey overview (PLAN.md §7/Ф20)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keeps import __version__, autostart, config, diagnostics, multi_paste, paste
from keeps.ai import download, models
from keeps.ai.runtime import AiRuntime
from keeps.hotkey.clips import ClipGlobalHotkeyManager
from keeps.store import Store

ABOUT_HTML = """\
<h3>Keeps {version}</h3>
<p>A Ditto-inspired clipboard manager for Linux.</p>
<p>License: GPL-3.0. Inspired by Ditto (GPL-3.0) &mdash; concepts and UX only, no shared code.</p>
<p><a href="https://github.com/GolovIaroslav/keeps">github.com/GolovIaroslav/keeps</a></p>
"""

# Static reference for the popup keymap (PLAN.md §6, normative) plus the
# right-click context menu, so the user can look this up inside the app
# instead of only in the README.
HOTKEYS_HTML = """\
<h3>Popup hotkeys</h3>
<table border="1" cellspacing="0" cellpadding="4">
<tr><th>Key</th><th>Action</th></tr>
<tr><td>Ctrl+` (global)</td><td>show/hide the popup</td></tr>
<tr><td>typing text</td><td>filter the list live</td></tr>
<tr><td>&uarr;/&darr;, PgUp/PgDn</td>
    <td>navigate (from the search field, arrows move the list)</td></tr>
<tr><td>Ctrl+A</td><td>select all visible search results</td></tr>
<tr><td>Enter / double-click</td>
    <td>paste the selected item; multiple items are joined as plain text</td></tr>
<tr><td>Shift+Enter</td><td>paste as plain text (also used for multiple items)</td></tr>
<tr><td>Ctrl+C</td><td>copy the selected item(s) to the clipboard only, no paste</td></tr>
<tr><td>Del</td><td>delete the selected item(s); asks for confirmation above 5</td></tr>
<tr><td>Ctrl+E</td>
    <td>edit externally (xdg-open on a temp file; saving updates the clip)</td></tr>
<tr><td>F3</td>
    <td>View: expand the selected item read-only (full text or full-size image)</td></tr>
<tr><td>F2</td><td>Edit: built-in editor (text clips only)</td></tr>
<tr><td>Ctrl+P</td><td>pin/unpin the selected item(s)</td></tr>
<tr><td>Ctrl+1..9</td><td>paste the Nth visible item (first 9 are numbered)</td></tr>
<tr><td>Ctrl+M</td><td>cycle search mode (blended/keywords/meaning) &mdash;
    only when semantic search is enabled</td></tr>
<tr><td>Ctrl+scroll, Ctrl+Plus, Ctrl+Minus</td>
    <td>popup UI scale (remembered between sessions)</td></tr>
<tr><td>Esc / focus loss</td><td>hide the popup</td></tr>
</table>
<p>The popup window can be dragged from its title bar and resized from any edge or corner.</p>
<p>Right-click menu: paste, paste as text, copy, View (F3), pin/unpin, Edit (F2, built-in,
text clips only), Edit externally (Ctrl+E), delete. Paste/copy/pin/delete act on the whole
selection; View and editing require one item. Plus a <b>Special Paste</b> submenu (one text
clip only): UPPERCASE / lowercase / Capitalize / Trim whitespace &mdash; pastes a transformed
copy without changing the stored clip; menu-only, no dedicated shortcut.</p>
"""


class _DownloadSignals(QObject):
    progress = Signal(int, int)  # (downloaded_bytes, total_bytes)
    finished = Signal(bool, str)  # (ok, error_message)


class _DownloadTask(QRunnable):
    """Runs off the main thread: fetch every file in one or more ModelSpecs
    (e.g. OCR's shared detector + one recognizer, downloaded together as a
    single user-facing "Download" action), verifying sha256 as it goes
    (ai/download.py). Never touches Store/Qt beyond emitting.
    """

    def __init__(self, specs: tuple[models.ModelSpec, ...], signals: _DownloadSignals) -> None:
        super().__init__()
        self._specs = specs
        self._signals = signals

    def run(self) -> None:
        try:
            for spec in self._specs:
                for file in spec.files:
                    dest = models.file_dest(spec, file)
                    if dest.is_file() and dest.stat().st_size == file.size_bytes:
                        continue  # already present -- e.g. the OCR detector,
                        # shared across every language's per-language download
                    download.download_file(
                        file.url,
                        dest,
                        file.sha256,
                        progress_cb=lambda done, total: self._signals.progress.emit(done, total),
                    )
            self._signals.finished.emit(True, "")
        except Exception as exc:  # surfaced in the UI, must not crash the daemon
            self._signals.finished.emit(False, str(exc))


class SettingsDialog(QDialog):
    def __init__(
        self,
        ai_runtime: AiRuntime | None = None,
        store: Store | None = None,
        parent: QWidget | None = None,
        *,
        clip_hotkeys: ClipGlobalHotkeyManager | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keeps Settings"))
        self._settings = config.open_settings()
        self._ai_runtime = ai_runtime
        self._store = store
        self._clip_hotkeys = clip_hotkeys

        tabs = QTabWidget(self)
        tabs.addTab(self._build_general_tab(), self.tr("General"))
        tabs.addTab(self._build_clip_hotkeys_tab(), self.tr("Clip hotkeys"))
        tabs.addTab(self._build_capture_tab(), self.tr("Capture"))
        tabs.addTab(self._build_ai_tab(), self.tr("AI"))
        tabs.addTab(self._build_diagnostics_tab(), self.tr("Diagnostics && About"))

        close_button = QPushButton(self.tr("Close"))
        close_button.clicked.connect(self.close)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(close_button)
        self.resize(480, 420)

    def _save(self, key: str, value) -> None:
        self._settings.setValue(key, value)

    # -- General -------------------------------------------------------

    def _build_general_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        max_items = QSpinBox()
        max_items.setRange(10, 100_000)
        max_items.setValue(int(config.get(self._settings, "general/max_items")))
        max_items.valueChanged.connect(lambda v: self._save("general/max_items", v))
        form.addRow(self.tr("Max history items (restart to apply)"), max_items)

        max_item_mb = QSpinBox()
        max_item_mb.setRange(1, 500)
        max_item_mb.setValue(int(config.get(self._settings, "general/max_item_mb")))
        max_item_mb.valueChanged.connect(lambda v: self._save("general/max_item_mb", v))
        form.addRow(self.tr("Max item size, MB (restart to apply)"), max_item_mb)

        hotkey = QLineEdit(str(config.get(self._settings, "general/hotkey")))
        hotkey.editingFinished.connect(lambda: self._save("general/hotkey", hotkey.text()))
        form.addRow(self.tr("Hotkey (restart to apply)"), hotkey)

        for key, label in (
            ("general/external_editor_text", self.tr("External editor — text")),
            ("general/external_editor_html", self.tr("External editor — HTML")),
            ("general/external_editor_image", self.tr("External editor — images")),
        ):
            editor = QLineEdit(str(config.get(self._settings, key)))
            editor.setPlaceholderText(self.tr("xdg-open (system default)"))
            editor.editingFinished.connect(lambda k=key, e=editor: self._save(k, e.text()))
            form.addRow(label, editor)

        theme_combo = QComboBox()
        theme_combo.addItems(["system", "light", "dark"])
        theme_combo.setCurrentText(str(config.get(self._settings, "general/theme")))
        theme_combo.currentTextChanged.connect(self._on_theme_changed)
        form.addRow(self.tr("Theme"), theme_combo)

        autostart_box = QCheckBox()
        autostart_box.setChecked(autostart.is_autostart_enabled())
        autostart_box.toggled.connect(autostart.set_autostart_enabled)
        form.addRow(self.tr("Start on login"), autostart_box)

        paste_enabled = QCheckBox()
        paste_enabled.setChecked(bool(config.get(self._settings, "paste/enabled")))
        paste_enabled.toggled.connect(lambda v: self._save("paste/enabled", v))
        form.addRow(self.tr("Auto-paste after selecting"), paste_enabled)

        keep_search = QCheckBox()
        keep_search.setChecked(
            bool(config.get(self._settings, "popup/keep_search_after_paste"))
        )
        keep_search.toggled.connect(
            lambda v: self._save("popup/keep_search_after_paste", v)
        )
        form.addRow(self.tr("Keep search after paste"), keep_search)

        multi_separator = QLineEdit(
            multi_paste.separator_to_display(
                str(config.get(self._settings, "paste/multi_separator"))
            )
        )
        multi_separator.setToolTip(self.tr(r"Use \n for a line break and \t for a tab"))
        multi_separator.editingFinished.connect(
            lambda: self._save(
                "paste/multi_separator",
                multi_paste.separator_from_display(multi_separator.text()),
            )
        )
        form.addRow(self.tr("Multi-paste separator"), multi_separator)

        reverse_multi = QCheckBox()
        reverse_multi.setChecked(
            bool(config.get(self._settings, "paste/multi_reverse_order"))
        )
        reverse_multi.toggled.connect(
            lambda v: self._save("paste/multi_reverse_order", v)
        )
        form.addRow(self.tr("Reverse multi-paste order"), reverse_multi)

        save_multi = QCheckBox()
        save_multi.setChecked(
            bool(config.get(self._settings, "paste/save_multi_as_clip"))
        )
        save_multi.toggled.connect(
            lambda v: self._save("paste/save_multi_as_clip", v)
        )
        form.addRow(self.tr("Save combined paste as a new clip"), save_multi)

        delay = QSpinBox()
        delay.setRange(0, 5000)
        delay.setSuffix(" ms")
        delay.setValue(int(config.get(self._settings, "paste/delay_ms")))
        delay.valueChanged.connect(lambda v: self._save("paste/delay_ms", v))
        form.addRow(self.tr("Paste delay"), delay)

        shortcuts_group = QGroupBox(self.tr("Per-app paste shortcuts"))
        shortcuts_layout = QVBoxLayout(shortcuts_group)
        shortcuts_help = QLabel(
            self.tr(
                "The active app is detected before the popup opens. "
                "Detection failure uses Ctrl+V."
            )
        )
        shortcuts_help.setWordWrap(True)
        shortcuts_layout.addWidget(shortcuts_help)
        shortcuts = QTableWidget(0, 2)
        shortcuts.setHorizontalHeaderLabels([self.tr("Application class"), self.tr("Shortcut")])
        shortcuts.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        shortcuts.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        mapping = paste.parse_app_shortcuts(
            str(config.get(self._settings, "paste/app_shortcuts"))
        )
        shortcuts.blockSignals(True)
        for app_class, shortcut in mapping.items():
            row = shortcuts.rowCount()
            shortcuts.insertRow(row)
            shortcuts.setItem(row, 0, QTableWidgetItem(app_class))
            shortcuts.setItem(row, 1, QTableWidgetItem(shortcut))
        shortcuts.blockSignals(False)

        def save_shortcuts() -> None:
            values = {}
            for row in range(shortcuts.rowCount()):
                app_item = shortcuts.item(row, 0)
                shortcut_item = shortcuts.item(row, 1)
                app_class = app_item.text().strip().casefold() if app_item else ""
                shortcut = shortcut_item.text().strip().casefold() if shortcut_item else ""
                if app_class and shortcut in {"ctrl+v", "ctrl+shift+v"}:
                    values[app_class] = shortcut
            self._save("paste/app_shortcuts", paste.format_app_shortcuts(values))

        shortcuts.itemChanged.connect(lambda _item: save_shortcuts())
        shortcuts_layout.addWidget(shortcuts)
        shortcut_buttons = QHBoxLayout()
        add_shortcut = QPushButton(self.tr("Add"))
        remove_shortcut = QPushButton(self.tr("Remove selected"))

        def add_shortcut_row() -> None:
            row = shortcuts.rowCount()
            shortcuts.insertRow(row)
            shortcuts.setItem(row, 0, QTableWidgetItem(""))
            shortcuts.setItem(row, 1, QTableWidgetItem("ctrl+shift+v"))
            shortcuts.setCurrentCell(row, 0)
            shortcuts.editItem(shortcuts.item(row, 0))

        def remove_shortcut_rows() -> None:
            for row in sorted({index.row() for index in shortcuts.selectedIndexes()}, reverse=True):
                shortcuts.removeRow(row)
            save_shortcuts()

        add_shortcut.clicked.connect(add_shortcut_row)
        remove_shortcut.clicked.connect(remove_shortcut_rows)
        shortcut_buttons.addWidget(add_shortcut)
        shortcut_buttons.addWidget(remove_shortcut)
        shortcut_buttons.addStretch(1)
        shortcuts_layout.addLayout(shortcut_buttons)
        form.addRow(shortcuts_group)

        return widget

    def _on_theme_changed(self, value: str) -> None:
        self._save("general/theme", value)
        config.apply_theme(value)

    # -- Clip hotkeys -------------------------------------------------

    def _build_clip_hotkeys_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        explanation = QLabel(
            self.tr(
                "Assign hotkeys in a clip's Properties. Local hotkeys work only while "
                "the popup is open; global hotkeys paste directly into the active app."
            )
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self._clip_hotkey_table = QTableWidget(0, 4)
        self._clip_hotkey_table.setHorizontalHeaderLabels(
            [self.tr("Clip"), self.tr("Hotkey"), self.tr("Scope"), self.tr("Action")]
        )
        self._clip_hotkey_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        for column in (1, 2, 3):
            self._clip_hotkey_table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(self._clip_hotkey_table)
        self._refresh_clip_hotkey_table()
        return widget

    def _refresh_clip_hotkey_table(self) -> None:
        table = self._clip_hotkey_table
        table.setRowCount(0)
        if self._store is None:
            return
        for clip in self._store.clips_with_hotkeys():
            row = table.rowCount()
            table.insertRow(row)
            title = clip.alias or clip.preview.replace("\n", " ") or self.tr("(empty clip)")
            table.setItem(row, 0, QTableWidgetItem(title))
            table.setItem(row, 1, QTableWidgetItem(clip.hotkey or ""))
            scope = self.tr("Global") if clip.hotkey_global else self.tr("Popup only")
            table.setItem(row, 2, QTableWidgetItem(scope))
            remove = QPushButton(self.tr("Remove"))
            remove.clicked.connect(
                lambda _checked=False, clip_id=clip.id: self._clear_hotkey(clip_id)
            )
            table.setCellWidget(row, 3, remove)

    def _clear_hotkey(self, clip_id: int) -> None:
        if self._store is None:
            return
        clip = next((clip for clip in self._store.all() if clip.id == clip_id), None)
        if clip is None:
            self._refresh_clip_hotkey_table()
            return
        if clip.hotkey_global and self._clip_hotkeys is not None:
            self._clip_hotkeys.unregister(clip_id)
        self._store.set_hotkey(clip_id, None, global_hotkey=False)
        self._refresh_clip_hotkey_table()

    # -- Capture ---------------------------------------------------------

    def _build_capture_tab(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)

        for key, label in (
            ("capture/store_html", self.tr("Store HTML clips")),
            ("capture/store_images", self.tr("Store image clips")),
            ("capture/store_files", self.tr("Store file-list clips")),
        ):
            box = QCheckBox()
            box.setChecked(bool(config.get(self._settings, key)))
            box.toggled.connect(lambda v, k=key: self._save(k, v))
            form.addRow(label, box)

        return widget

    # -- AI ----------------------------------------------------------------

    def _build_ai_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Three independent toggles (PLAN.md §9) -- plain substring search is
        # never gated by any of these, so there's no "AI on/off" master switch.
        toggles = QFormLayout()

        rag_box = QCheckBox()
        rag_box.setChecked(bool(config.get(self._settings, "ai/rag_text_enabled")))
        rag_box.toggled.connect(self._on_rag_toggled)
        toggles.addRow(self.tr("Semantic search (RAG) over clip text"), rag_box)

        ocr_box = QCheckBox()
        ocr_box.setChecked(bool(config.get(self._settings, "ai/ocr_enabled")))
        ocr_box.toggled.connect(self._on_ocr_toggled)
        toggles.addRow(self.tr("OCR text from image clips"), ocr_box)

        image_semantic_status = QLabel(
            self.tr("Image semantic search — experimental, not implemented yet")
        )
        image_semantic_status.setEnabled(False)
        image_semantic_status.setWordWrap(True)
        toggles.addRow(image_semantic_status)
        layout.addLayout(toggles)

        timing = QFormLayout()
        timing_combo = QComboBox()
        timing_combo.addItems(["immediate", "delayed", "scheduled"])
        timing_combo.setCurrentText(str(config.get(self._settings, "ai/ocr_timing")))
        timing_combo.currentTextChanged.connect(lambda v: self._save("ai/ocr_timing", v))
        timing.addRow(self.tr("OCR timing"), timing_combo)

        delay_spin = QSpinBox()
        delay_spin.setRange(1, 600)
        delay_spin.setSuffix(" s")
        delay_spin.setValue(int(config.get(self._settings, "ai/ocr_delay_seconds")))
        delay_spin.valueChanged.connect(lambda v: self._save("ai/ocr_delay_seconds", v))
        delay_spin.setEnabled(timing_combo.currentText() == "delayed")
        timing_combo.currentTextChanged.connect(lambda v: delay_spin.setEnabled(v == "delayed"))
        timing.addRow(self.tr("OCR delay (debounced from the last capture)"), delay_spin)

        idle_spin = QSpinBox()
        idle_spin.setRange(0, 1440)
        idle_spin.setSuffix(" min")
        idle_spin.setSpecialValueText(self.tr("never"))
        idle_spin.setValue(int(config.get(self._settings, "ai/model_idle_unload_minutes")))
        idle_spin.valueChanged.connect(lambda v: self._save("ai/model_idle_unload_minutes", v))
        timing.addRow(self.tr("Unload model from RAM after idle"), idle_spin)
        layout.addLayout(timing)

        management_label = QLabel(f"<b>{self.tr('Model management')}</b>")
        layout.addWidget(management_label)

        if self._ai_runtime is not None:
            layout.addWidget(
                self._build_model_section(
                    self.tr("Text embeddings"),
                    (models.TEXT_EMBED,),
                    self._ai_runtime.text_embed_status,
                    self._ai_runtime.load_text_embedder,
                    self._ai_runtime.unload_text_embedder,
                )
            )
        layout.addWidget(self._build_ocr_languages_section())
        layout.addWidget(
            self._build_model_section(self.tr("Image-semantic search"), None, None, None, None)
        )

        layout.addStretch(1)
        return widget

    def _on_ocr_toggled(self, checked: bool) -> None:
        self._save("ai/ocr_enabled", checked)
        if checked and self._ai_runtime is not None:
            self._ai_runtime.run_ocr_backlog_sweep()

    def _on_rag_toggled(self, checked: bool) -> None:
        self._save("ai/rag_text_enabled", checked)
        if checked and self._ai_runtime is not None:
            self._ai_runtime.run_text_embed_backlog_sweep()

    def _build_model_section(
        self,
        title: str,
        specs: tuple[models.ModelSpec, ...] | None,
        status_fn,
        load_fn,
        unload_fn,
    ) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)

        if not specs:
            layout.addWidget(QLabel(self.tr("Not implemented yet.")))
            return box

        path_label = QLabel()
        path_label.setWordWrap(True)
        status_label = QLabel()
        progress = QProgressBar()
        progress.setVisible(False)
        layout.addWidget(path_label)
        layout.addWidget(status_label)
        layout.addWidget(progress)

        buttons = QHBoxLayout()
        download_btn = QPushButton(self.tr("Download"))
        delete_btn = QPushButton(self.tr("Delete from disk"))
        load_btn = QPushButton(self.tr("Load into RAM"))
        unload_btn = QPushButton(self.tr("Unload from RAM"))
        for button in (download_btn, delete_btn, load_btn, unload_btn):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        state = {"downloading": False}

        total_size = sum(spec.total_size_bytes for spec in specs)
        dirs = ", ".join(str(models.model_dir(spec)) for spec in specs)

        def refresh() -> None:
            status = models.ModelStatus.DOWNLOADING if state["downloading"] else status_fn()
            path_label.setText(f"{dirs} ({models.human_size(total_size)})")
            status_label.setText(self.tr("Status: {status}").format(status=status.value))
            progress.setVisible(status == models.ModelStatus.DOWNLOADING)
            download_btn.setEnabled(status == models.ModelStatus.NOT_DOWNLOADED)
            delete_btn.setEnabled(
                status in (models.ModelStatus.DOWNLOADED, models.ModelStatus.LOADED)
            )
            load_btn.setEnabled(status == models.ModelStatus.DOWNLOADED)
            unload_btn.setEnabled(status == models.ModelStatus.LOADED)

        def on_progress(done: int, total: int) -> None:
            progress.setValue(int(done / total * 100) if total else 0)

        def on_finished(ok: bool, error: str) -> None:
            state["downloading"] = False
            refresh()
            if not ok:
                status_label.setText(self.tr("Download failed: {error}").format(error=error))

        def on_download() -> None:
            state["downloading"] = True
            refresh()
            signals = _DownloadSignals(self)
            signals.progress.connect(on_progress)
            signals.finished.connect(on_finished)
            QThreadPool.globalInstance().start(_DownloadTask(specs, signals))

        def on_delete() -> None:
            for spec in specs:
                models.delete_files(spec)
            refresh()

        def on_load() -> None:
            load_fn()
            refresh()

        def on_unload() -> None:
            unload_fn()
            refresh()

        download_btn.clicked.connect(on_download)
        delete_btn.clicked.connect(on_delete)
        load_btn.clicked.connect(on_load)
        unload_btn.clicked.connect(on_unload)

        refresh()
        return box

    def _build_ocr_languages_section(self) -> QGroupBox:
        """One checkable row per OCR recognizer language (Ф9.6): fully user-
        driven, no language is favored over another beyond the shipped
        default (ai/ocr_languages="eslav", preserving pre-Ф9.6 behavior).
        Checking a not-yet-downloaded language auto-downloads it (queued one
        at a time); unchecking only updates the selection, it never deletes
        weights already on disk.
        """
        box = QGroupBox(self.tr("OCR languages"))
        layout = QVBoxLayout(box)

        list_widget = QListWidget()
        codes = sorted(models.OCR_REC)  # deterministic order
        selected = set(
            config.parse_ocr_languages(str(config.get(self._settings, "ai/ocr_languages")))
        )

        status_label = QLabel()
        progress = QProgressBar()
        progress.setVisible(False)
        warning_label = QLabel()
        warning_label.setWordWrap(True)

        items: dict[str, QListWidgetItem] = {}
        for code in codes:
            item = QListWidgetItem()
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setCheckState(
                Qt.CheckState.Checked if code in selected else Qt.CheckState.Unchecked
            )
            list_widget.addItem(item)
            items[code] = item
        layout.addWidget(list_widget)
        layout.addWidget(status_label)
        layout.addWidget(progress)
        layout.addWidget(warning_label)

        queue: list[str] = []
        state = {"downloading": None}  # currently-downloading code, or None

        def item_status_text(code: str) -> str:
            if code == state["downloading"]:
                return self.tr("downloading...")
            if models.is_downloaded(models.OCR_REC[code]):
                return self.tr("downloaded")
            return self.tr("not downloaded")

        def refresh_item(code: str) -> None:
            spec = models.OCR_REC[code]
            base = f"{spec.label} ({models.human_size(spec.total_size_bytes)})"
            # setText() on a QListWidgetItem fires itemChanged too, not just
            # check-state edits -- without blocking, this programmatic refresh
            # would re-enter on_item_changed() and could re-trigger a download
            # (found live via the offscreen smoke test: refreshing the default
            # checked-but-not-yet-downloaded item spuriously kicked off a real
            # network fetch on construction).
            list_widget.blockSignals(True)
            items[code].setText(f"{base} — {item_status_text(code)}")
            list_widget.blockSignals(False)

        def refresh_warning() -> None:
            warning_label.setVisible(len(selected) > 3)
            if len(selected) > 3:
                warning_label.setText(
                    self.tr(
                        "{n} languages selected: OCR runs a separate recognition pass "
                        "per selected language for every detected text region, so more "
                        "languages means proportionally slower OCR on each screenshot."
                    ).format(n=len(selected))
                )

        def refresh_all() -> None:
            for code in codes:
                refresh_item(code)
            refresh_warning()

        def start_next_download() -> None:
            if state["downloading"] is not None or not queue:
                return
            code = queue.pop(0)
            state["downloading"] = code
            refresh_item(code)
            status_label.setText(
                self.tr("Downloading {label}...").format(label=models.OCR_REC[code].label)
            )
            progress.setVisible(True)
            progress.setValue(0)

            signals = _DownloadSignals(self)
            signals.progress.connect(
                lambda done, total: progress.setValue(int(done / total * 100) if total else 0)
            )
            signals.finished.connect(
                lambda ok, error, code=code: on_download_finished(code, ok, error)
            )
            QThreadPool.globalInstance().start(
                _DownloadTask((models.OCR_DET, models.OCR_REC[code]), signals)
            )

        def on_download_finished(code: str, ok: bool, error: str) -> None:
            state["downloading"] = None
            progress.setVisible(False)
            if ok:
                status_label.setText(
                    self.tr("Downloaded {label}").format(label=models.OCR_REC[code].label)
                )
                if self._ai_runtime is not None:
                    self._ai_runtime.reset_ocr_engine()
            else:
                status_label.setText(self.tr("Download failed: {error}").format(error=error))
            refresh_item(code)
            start_next_download()

        def on_item_changed(item: QListWidgetItem) -> None:
            code = item.data(Qt.ItemDataRole.UserRole)
            checked = item.checkState() == Qt.CheckState.Checked
            if checked:
                selected.add(code)
            else:
                selected.discard(code)
            self._save(
                "ai/ocr_languages",
                config.format_ocr_languages([c for c in codes if c in selected]),
            )
            if self._ai_runtime is not None:
                self._ai_runtime.reset_ocr_engine()
            if (
                checked
                and not models.is_downloaded(models.OCR_REC[code])
                and code != state["downloading"]
                and code not in queue
            ):
                queue.append(code)
                start_next_download()
            refresh_all()

        list_widget.itemChanged.connect(on_item_changed)
        refresh_all()
        return box

    # -- Diagnostics & About ------------------------------------------------

    def _build_diagnostics_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self._diagnostics_list = QListWidget()
        layout.addWidget(self._diagnostics_list)

        refresh_button = QPushButton(self.tr("Run checks"))
        refresh_button.clicked.connect(self._refresh_diagnostics)
        layout.addWidget(refresh_button)

        layout.addWidget(self._build_db_info_group())

        hotkeys = QLabel(HOTKEYS_HTML)
        hotkeys.setWordWrap(True)
        layout.addWidget(hotkeys)

        about = QLabel(ABOUT_HTML.format(version=__version__))
        about.setOpenExternalLinks(True)
        about.setWordWrap(True)
        layout.addWidget(about)

        self._refresh_diagnostics()
        return widget

    def _build_db_info_group(self) -> QGroupBox:
        """DB path/size + trim explanation (PLAN.md §9.5) + Ф10 maintenance
        buttons (Backup now / Compact / Clear history). Size is computed
        fresh here so it can't go stale across dialog opens the way a value
        cached at import time would.
        """
        group = QGroupBox(self.tr("Database"))
        form = QFormLayout(group)

        db_path = self._store.db_path if self._store is not None else config.default_db_path()
        path_label = QLabel(str(db_path))
        path_label.setWordWrap(True)
        form.addRow(self.tr("Path"), path_label)

        self._db_size_label = QLabel()
        form.addRow(self.tr("Size"), self._db_size_label)
        self._refresh_db_size_label()

        max_items = int(config.get(self._settings, "general/max_items"))
        cleanup_label = QLabel(
            self.tr(
                "Trimmed after every new capture: the oldest unpinned clips beyond "
                "the configured limit (currently {max_items}) are deleted. Pinned "
                "clips are never removed by this cleanup, no matter how many are pinned."
            ).format(max_items=max_items)
        )
        cleanup_label.setWordWrap(True)
        form.addRow(cleanup_label)

        if self._store is not None:
            button_row = QHBoxLayout()
            backup_button = QPushButton(self.tr("Backup now"))
            backup_button.clicked.connect(self._on_backup_now)
            compact_button = QPushButton(self.tr("Compact (VACUUM)"))
            compact_button.clicked.connect(self._on_compact)
            clear_button = QPushButton(self.tr("Clear history…"))
            clear_button.clicked.connect(self._on_clear_history)
            button_row.addWidget(backup_button)
            button_row.addWidget(compact_button)
            button_row.addWidget(clear_button)
            form.addRow(button_row)

        return group

    def _refresh_db_size_label(self) -> None:
        db_path = self._store.db_path if self._store is not None else config.default_db_path()
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
        self._db_size_label.setText(models.human_size(size_bytes))

    def _on_backup_now(self) -> None:
        backup_path = self._store.backup_now()
        QMessageBox.information(
            self,
            self.tr("Backup created"),
            self.tr("Saved to:\n{path}").format(path=backup_path),
        )

    def _on_compact(self) -> None:
        before, after = self._store.compact()
        QMessageBox.information(
            self,
            self.tr("Compact complete"),
            self.tr("Size before: {before}\nSize after: {after}").format(
                before=models.human_size(before), after=models.human_size(after)
            ),
        )
        self._refresh_db_size_label()

    def _on_clear_history(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Clear history"))
        box.setText(self.tr("Delete all clips? This cannot be undone."))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        include_pinned_box = QCheckBox(self.tr("Also delete pinned clips"))
        box.setCheckBox(include_pinned_box)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        clips_to_delete = [
            clip
            for clip in self._store.all()
            if include_pinned_box.isChecked() or not clip.pinned
        ]
        deleted = self._store.clear_history(include_pinned=include_pinned_box.isChecked())
        if self._clip_hotkeys is not None:
            for clip in clips_to_delete:
                if clip.hotkey_global:
                    self._clip_hotkeys.unregister(clip.id)
        self._refresh_clip_hotkey_table()
        QMessageBox.information(
            self,
            self.tr("History cleared"),
            self.tr("{count} clip(s) deleted.").format(count=deleted),
        )
        self._refresh_db_size_label()

    def _refresh_diagnostics(self) -> None:
        self._diagnostics_list.clear()
        checks = diagnostics.run_all(shutil.which, subprocess.run, Path.exists)
        for check in checks:
            mark = "✓" if check.ok else "✗"
            self._diagnostics_list.addItem(f"{mark} {check.name}: {check.detail}")
