"""System diagnostics checks (PLAN.md §8), shared by `keeps status` and the
Settings > Diagnostics tab.

No PySide6/QtDBus imports: the KGlobalAccel reachability check shells out to
`busctl` instead (see hotkey/kglobalaccel.py for why QtDBus is avoided for
D-Bus calls with array arguments; here it's simply to keep this module -- and
its tests -- Qt-free, consistent with ui/format.py and paste.py).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def check_wl_paste(which: Callable[[str], str | None]) -> Check:
    found = which("wl-paste") is not None
    return Check("wl-paste", found, "found" if found else "install wl-clipboard")


def check_paste_injector(env: dict[str, str], which: Callable[[str], str | None]) -> Check:
    tool = "ydotool" if env.get("XDG_SESSION_TYPE") == "wayland" else "xdotool"
    found = which(tool) is not None
    return Check(tool, found, "found" if found else f"install {tool} for auto-paste")


def check_uinput_access(
    path_exists: Callable[[Path], bool], path: Path = Path("/dev/uinput")
) -> Check:
    accessible = path_exists(path)
    detail = "accessible" if accessible else "start ydotoold and check /dev/uinput udev rules"
    return Check("uinput", accessible, detail)


def check_session_type(env: dict[str, str]) -> Check:
    session = env.get("XDG_SESSION_TYPE", "")
    ok = session in ("wayland", "x11")
    return Check("session type", ok, session or "unknown")


def check_kglobalaccel(runner: Callable[..., subprocess.CompletedProcess]) -> Check:
    try:
        result = runner(
            ["busctl", "--user", "introspect", "org.kde.kglobalaccel", "/kglobalaccel"],
            capture_output=True,
            timeout=2,
        )
        ok = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        ok = False
    detail = "responding" if ok else "not available (hotkey needs KDE Plasma's kglobalaccel)"
    return Check("kglobalaccel D-Bus", ok, detail)


def check_klipper(runner: Callable[..., subprocess.CompletedProcess]) -> Check:
    try:
        result = runner(["pgrep", "-x", "klipper"], capture_output=True, timeout=2)
        running = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        running = False
    detail = (
        "running -- disable Klipper's clipboard history to avoid clashing with Keeps"
        if running
        else "not running"
    )
    return Check("Klipper", not running, detail)


def check_tesseract(which: Callable[[str], str | None]) -> Check:
    found = which("tesseract") is not None
    detail = "found" if found else "optional: install for OCR search"
    return Check("tesseract (AI/OCR)", found, detail)


def run_all(
    which: Callable[[str], str | None],
    runner: Callable[..., subprocess.CompletedProcess],
    path_exists: Callable[[Path], bool],
    env: dict[str, str] | None = None,
) -> list[Check]:
    env = env if env is not None else dict(os.environ)
    return [
        check_wl_paste(which),
        check_paste_injector(env, which),
        check_uinput_access(path_exists),
        check_session_type(env),
        check_kglobalaccel(runner),
        check_klipper(runner),
        check_tesseract(which),
    ]
