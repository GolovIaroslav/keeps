"""Settings dialog: General / Capture / AI / Diagnostics & About (PLAN.md §7)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keeps import __version__, autostart, config, diagnostics
from keeps.ai import download, models
from keeps.ai.runtime import AiRuntime

ABOUT_HTML = """\
<h3>Keeps {version}</h3>
<p>A Ditto-inspired clipboard manager for Linux.</p>
<p>License: GPL-3.0. Inspired by Ditto (GPL-3.0) &mdash; concepts and UX only, no shared code.</p>
<p><a href="https://github.com/GolovIaroslav/keeps">github.com/GolovIaroslav/keeps</a></p>
"""


class _DownloadSignals(QObject):
    progress = Signal(int, int)  # (downloaded_bytes, total_bytes)
    finished = Signal(bool, str)  # (ok, error_message)


class _DownloadTask(QRunnable):
    """Runs off the main thread: fetch every file in a ModelSpec, verifying
    sha256 as it goes (ai/download.py). Never touches Store/Qt beyond emitting.
    """

    def __init__(self, spec: models.ModelSpec, signals: _DownloadSignals) -> None:
        super().__init__()
        self._spec = spec
        self._signals = signals

    def run(self) -> None:
        try:
            for file in self._spec.files:
                dest = models.file_dest(self._spec, file)
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
    def __init__(self, ai_runtime: AiRuntime | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keeps Settings"))
        self._settings = config.open_settings()
        self._ai_runtime = ai_runtime

        tabs = QTabWidget(self)
        tabs.addTab(self._build_general_tab(), self.tr("General"))
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

        delay = QSpinBox()
        delay.setRange(0, 5000)
        delay.setSuffix(" ms")
        delay.setValue(int(config.get(self._settings, "paste/delay_ms")))
        delay.valueChanged.connect(lambda v: self._save("paste/delay_ms", v))
        form.addRow(self.tr("Paste delay"), delay)

        return widget

    def _on_theme_changed(self, value: str) -> None:
        self._save("general/theme", value)
        config.apply_theme(value)

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

        image_semantic_box = QCheckBox()
        image_semantic_box.setChecked(bool(config.get(self._settings, "ai/image_semantic_enabled")))
        image_semantic_box.toggled.connect(lambda v: self._save("ai/image_semantic_enabled", v))
        toggles.addRow(
            self.tr("Semantic search over image content (not implemented yet)"),
            image_semantic_box,
        )
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
                    models.TEXT_EMBED,
                    self._ai_runtime.text_embed_status,
                    self._ai_runtime.load_text_embedder,
                    self._ai_runtime.unload_text_embedder,
                )
            )
            layout.addWidget(
                self._build_model_section(
                    self.tr("OCR"),
                    models.OCR,
                    self._ai_runtime.ocr_status,
                    self._ai_runtime.load_ocr_engine,
                    self._ai_runtime.unload_ocr_engine,
                )
            )
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
        spec: models.ModelSpec | None,
        status_fn,
        load_fn,
        unload_fn,
    ) -> QGroupBox:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)

        if spec is None:
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

        def refresh() -> None:
            status = models.ModelStatus.DOWNLOADING if state["downloading"] else status_fn()
            path_label.setText(
                f"{models.model_dir(spec)} ({models.human_size(spec.total_size_bytes)})"
            )
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
            QThreadPool.globalInstance().start(_DownloadTask(spec, signals))

        def on_delete() -> None:
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

    # -- Diagnostics & About ------------------------------------------------

    def _build_diagnostics_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        self._diagnostics_list = QListWidget()
        layout.addWidget(self._diagnostics_list)

        refresh_button = QPushButton(self.tr("Run checks"))
        refresh_button.clicked.connect(self._refresh_diagnostics)
        layout.addWidget(refresh_button)

        about = QLabel(ABOUT_HTML.format(version=__version__))
        about.setOpenExternalLinks(True)
        about.setWordWrap(True)
        layout.addWidget(about)

        self._refresh_diagnostics()
        return widget

    def _refresh_diagnostics(self) -> None:
        self._diagnostics_list.clear()
        checks = diagnostics.run_all(shutil.which, subprocess.run, Path.exists)
        for check in checks:
            mark = "✓" if check.ok else "✗"
            self._diagnostics_list.addItem(f"{mark} {check.name}: {check.detail}")
