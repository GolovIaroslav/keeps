"""Shared QSettings wrapper: path resolution + defaults (PLAN.md §7, normative)."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSettings

from keeps.popup_keymap import DEFAULT_POPUP_KEYBINDINGS, setting_key

DEFAULTS: dict[str, bool | int | str] = {
    "general/max_items": 500,
    "general/max_item_mb": 10,
    "general/autostart": True,
    "general/hotkey": "Ctrl+`",
    "general/theme": "system",
    "general/external_editor_text": "",
    "general/external_editor_html": "",
    "general/external_editor_image": "",
    "paste/delay_ms": 150,
    "paste/enabled": True,
    "paste/multi_separator": "\n",
    "paste/multi_reverse_order": False,
    "paste/save_multi_as_clip": False,
    "paste/app_shortcuts": (
        '{"alacritty":"ctrl+shift+v","com.mitchellh.ghostty":"ctrl+shift+v",'
        '"foot":"ctrl+shift+v","ghostty":"ctrl+shift+v","kitty":"ctrl+shift+v",'
        '"konsole":"ctrl+shift+v","org.kde.konsole":"ctrl+shift+v",'
        '"org.wezfurlong.wezterm":"ctrl+shift+v","wezterm":"ctrl+shift+v",'
        '"xterm":"ctrl+shift+v","yakuake":"ctrl+shift+v"}'
    ),
    "popup/keep_search_after_paste": False,
    "buffers/1/copy_hotkey": "",
    "buffers/1/paste_hotkey": "",
    "buffers/2/copy_hotkey": "",
    "buffers/2/paste_hotkey": "",
    "buffers/3/copy_hotkey": "",
    "buffers/3/paste_hotkey": "",
    "capture/store_html": True,
    "capture/store_images": True,
    "capture/store_files": True,
    "ai/rag_text_enabled": False,
    "ai/ocr_enabled": False,
    "ai/image_semantic_enabled": False,
    "ai/ocr_timing": "delayed",
    "ai/ocr_delay_seconds": 10,
    "ai/model_idle_unload_minutes": 10,
    # Comma-joined language codes (keys of ai.models.OCR_REC), not a Python
    # list: QSettings/INI has no clean round-trip for list-typed values, so
    # this is stored as a plain string like every other DEFAULTS entry here.
    # "eslav" alone reproduces today's shipped (pre-Ф9.6) behavior exactly --
    # no bias toward any other language beyond preserving that default.
    "ai/ocr_languages": "eslav",
    **{setting_key(action): sequence for action, sequence in DEFAULT_POPUP_KEYBINDINGS.items()},
}


def settings_path() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = config_home / "keeps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "keeps.ini"


def default_db_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    directory = data_home / "keeps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "keeps.db"


def open_settings() -> QSettings:
    return QSettings(str(settings_path()), QSettings.Format.IniFormat)


def get(settings: QSettings, key: str):
    default = DEFAULTS[key]
    return settings.value(key, default, type=type(default))


def parse_ocr_languages(value: str) -> list[str]:
    """Comma-separated language codes -> a list: trimmed, empty entries
    dropped, order preserved, de-duplicated (first occurrence wins).
    """
    codes: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        code = raw.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes


def format_ocr_languages(codes: list[str]) -> str:
    """Inverse of parse_ocr_languages: comma-join, no extra whitespace."""
    return ",".join(codes)


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
