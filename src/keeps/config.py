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
    "paste/delay_ms": 150,
    "paste/enabled": True,
    "capture/store_html": True,
    "capture/store_images": True,
    "capture/store_files": True,
    "ai/ocr_enabled": False,
    "ai/semantic_enabled": False,
    "ai/ocr_langs": "rus+eng",
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
