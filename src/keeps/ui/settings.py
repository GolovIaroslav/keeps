"""Settings dialog: General / Capture / AI / Diagnostics & About (PLAN.md §7)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keeps import __version__, autostart, config, diagnostics

ABOUT_HTML = """\
<h3>Keeps {version}</h3>
<p>A Ditto-inspired clipboard manager for Linux.</p>
<p>License: GPL-3.0. Inspired by Ditto (GPL-3.0) &mdash; concepts and UX only, no shared code.</p>
<p><a href="https://github.com/GolovIaroslav/keeps">github.com/GolovIaroslav/keeps</a></p>
"""


class SettingsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Keeps Settings"))
        self._settings = config.open_settings()

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

        note = QLabel(
            self.tr(
                "AI-powered OCR and semantic search are not implemented yet "
                "(planned for a future release). These settings are saved "
                "but have no effect for now."
            )
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        form = QFormLayout()
        ocr_box = QCheckBox()
        ocr_box.setChecked(bool(config.get(self._settings, "ai/ocr_enabled")))
        ocr_box.toggled.connect(lambda v: self._save("ai/ocr_enabled", v))
        form.addRow(self.tr("Enable OCR"), ocr_box)

        semantic_box = QCheckBox()
        semantic_box.setChecked(bool(config.get(self._settings, "ai/semantic_enabled")))
        semantic_box.toggled.connect(lambda v: self._save("ai/semantic_enabled", v))
        form.addRow(self.tr("Enable semantic search"), semantic_box)

        langs = QLineEdit(str(config.get(self._settings, "ai/ocr_langs")))
        langs.editingFinished.connect(lambda: self._save("ai/ocr_langs", langs.text()))
        form.addRow(self.tr("OCR languages"), langs)

        layout.addLayout(form)
        layout.addStretch(1)
        return widget

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
