"""Installs a real Applications-menu .desktop entry for Keeps.

Without this, KDE has no installed application to associate the running
process with, so KGlobalAccel's Shortcuts KCM buckets our shortcut under a
generic "System services" heading instead of showing it as a proper
Application with an icon (unlike `autostart.py`'s entry, which is
`NoDisplay=true` and lives in `~/.config/autostart/`, not meant to be seen
in an app menu).
"""

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
Categories=Utility;
"""


def applications_path(data_home: Path | None = None) -> Path:
    base = data_home or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "applications" / "keeps.desktop"


def ensure_installed(data_home: Path | None = None) -> None:
    path = applications_path(data_home)
    if path.exists() and path.read_text() == DESKTOP_ENTRY:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DESKTOP_ENTRY)
