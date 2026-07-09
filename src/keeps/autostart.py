"""Autostart via the XDG autostart spec: ~/.config/autostart/keeps.desktop."""

from __future__ import annotations

import os
from pathlib import Path

DESKTOP_ENTRY = """\
[Desktop Entry]
Type=Application
Name=Keeps
Comment=Clipboard manager
Exec=keeps
Icon=edit-paste
X-GNOME-Autostart-enabled=true
NoDisplay=true
"""


def autostart_path(config_home: Path | None = None) -> Path:
    base = config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart" / "keeps.desktop"


def is_autostart_enabled(config_home: Path | None = None) -> bool:
    return autostart_path(config_home).exists()


def set_autostart_enabled(enabled: bool, config_home: Path | None = None) -> None:
    path = autostart_path(config_home)
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DESKTOP_ENTRY)
    else:
        path.unlink(missing_ok=True)
