"""Installs a real Applications-menu .desktop entry for Keeps.

Without this, KDE has no installed application to associate the running
process with, so KGlobalAccel's Shortcuts KCM buckets our shortcut under a
generic "System services" heading instead of showing it as a proper
Application with an icon (unlike `autostart.py`'s entry, which is
`NoDisplay=true` and lives in `~/.config/autostart/`, not meant to be seen
in an app menu).

This covers people running Keeps from a source checkout (`uv run keeps`)
and from an AppImage -- in both cases no package manager installs a desktop
file, and `Exec=` must point at what actually launches this instance (see
`launch_command`). Distro packages (Ф8: AUR) ship the static
`packaging/keeps.desktop` instead -- see PLAN.md §10; keep the template
below in sync with that file by hand.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

DESKTOP_ENTRY_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Keeps
Comment=Clipboard manager
Exec={exec_command}
Icon=edit-paste
Categories=Utility;
StartupWMClass=keeps
StartupNotify=true
"""


def launch_command(
    environ: dict[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    argv0: str | None = None,
) -> str:
    """The command a .desktop Exec= line can use to start Keeps.

    A bare `keeps` only works when the entry point is on PATH (e.g. a distro
    package). Running from an AppImage the real launchable thing is the image
    itself ($APPIMAGE, set by the AppImage runtime); running from a source
    checkout it is the venv's console script (sys.argv[0]).

    The absolute-path check runs *before* the `which()` check: `uv run keeps`
    also makes `which("keeps")` succeed, since uv prepends the venv's bin dir
    to PATH for that one child process only -- trusting it there would write
    a bare `Exec=keeps` that fails from any future launch context (a plain
    login shell, the Applications menu) whose PATH doesn't have that venv on
    it. An absolute path is always launchable regardless of PATH, so it's
    preferred whenever sys.argv[0] resolves to a real file; `which()` is only
    the fallback for the cases where it doesn't (e.g. `python -m keeps`).
    """
    env = os.environ if environ is None else environ
    appimage = env.get("APPIMAGE")
    if appimage:
        return appimage
    candidate = Path(argv0 if argv0 is not None else sys.argv[0])
    if candidate.name == "keeps" and candidate.is_file():
        return str(candidate.resolve())
    if which("keeps"):
        return "keeps"
    return "keeps"


def render_desktop_entry() -> str:
    return DESKTOP_ENTRY_TEMPLATE.format(exec_command=launch_command())


def applications_path(data_home: Path | None = None) -> Path:
    base = data_home or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "applications" / "keeps.desktop"


def ensure_installed(data_home: Path | None = None) -> None:
    entry = render_desktop_entry()
    path = applications_path(data_home)
    if path.exists() and path.read_text() == entry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(entry)
