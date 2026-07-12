"""Shared QSettings wrapper: path resolution + defaults (PLAN.md §7, normative)."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings

DEFAULTS: dict[str, bool | int | str] = {
    "general/max_items": 500,
    "general/max_item_mb": 10,
    "general/autostart": True,
    "general/hotkey": "Ctrl+`",
    "general/theme": "system",
    "paste/delay_ms": 150,
    "paste/enabled": True,
    "capture/store_html": True,
    "capture/store_images": True,
    "capture/store_files": True,
    "ai/rag_text_enabled": False,
    "ai/ocr_enabled": False,
    "ai/image_semantic_enabled": False,
    "ai/ocr_timing": "delayed",
    "ai/ocr_delay_seconds": 10,
    "ai/model_idle_unload_minutes": 10,
}


def settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = config_home / "keeps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "keeps.ini"


def open_settings() -> QSettings:
    return QSettings(str(settings_path()), QSettings.Format.IniFormat)


def get(settings: QSettings, key: str):
    default = DEFAULTS[key]
    return settings.value(key, default, type=type(default))


def apply_theme(theme: str) -> None:
    """Apply general/theme to the running app's color scheme (PLAN.md §7).

    "system" un-forces any previously set scheme so the app follows the
    platform theme (today's behavior, unchanged). Called once at daemon
    startup and again immediately whenever the Settings dialog changes it,
    so the effect is live without a restart.

    QtGui is imported here, not at module scope: this module is imported by
    headless-CI-safe code (e.g. ai/runtime.py, confirmed QtCore-only-safe
    without libEGL, see tests/test_ai_runtime.py) and libQt6Gui.so links
    against libEGL.so.1, which a minimal CI runner may not have installed --
    a module-level import would make merely importing keeps.config fail
    there (same class of bug as the delegate.py/test_delegate.py CI break).
    """
    from PySide6.QtGui import QGuiApplication, Qt

    style_hints = QGuiApplication.styleHints()
    if theme == "light":
        style_hints.setColorScheme(Qt.ColorScheme.Light)
    elif theme == "dark":
        style_hints.setColorScheme(Qt.ColorScheme.Dark)
    else:
        style_hints.unsetColorScheme()
