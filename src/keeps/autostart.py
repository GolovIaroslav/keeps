"""Autostart via the XDG autostart spec: ~/.config/autostart/keeps.desktop."""

from __future__ import annotations

import os
from pathlib import Path

from keeps.desktop_entry import launch_command

DESKTOP_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Keeps
Comment=Clipboard manager
Exec={exec_command}
Icon=edit-paste
StartupWMClass=keeps
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
        path.write_text(DESKTOP_ENTRY_TEMPLATE.format(exec_command=launch_command()))
    else:
        path.unlink(missing_ok=True)


def refresh_if_enabled(config_home: Path | None = None) -> None:
    """Rewrite the autostart entry with the current launch command, if enabled.

    AppImage filenames carry the version (`keeps-0.3.0-x86_64.AppImage`), so an
    entry written by `set_autostart_enabled` at one version points at a file
    that no longer exists after the next update -- silently breaking
    login-autostart. Called on every daemon startup, mirroring how
    `desktop_entry.ensure_installed` keeps the Applications-menu entry current.
    """
    if is_autostart_enabled(config_home):
        set_autostart_enabled(True, config_home)
