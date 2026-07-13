"""Paste injection: replay Ctrl+V into whatever window regains focus after the popup hides.

No Qt imports here (see ui/format.py for why: keeps pytest collection Qt-free
on headless CI). Delay scheduling and QSettings lookups live in the caller.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable

logger = logging.getLogger(__name__)

# ydotool >=1.0 dropped named-key syntax ("ctrl+v") in favor of raw
# input-event-codes.h keycodes: KEY_LEFTCTRL=29, KEY_V=47. Confirmed live
# against ydotool 1.0.4 on the user's machine per PLAN.md §11 (named syntax
# errors with "Unknown command"; this raw press/release sequence works).
YDOTOOL_CTRL_V = ["29:1", "47:1", "47:0", "29:0"]
YDOTOOL_CTRL_SHIFT_V = ["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"]

# Defense in depth: inject_paste() runs off the Qt main thread (see
# ui/popup.py::_PasteInjectionTask), but a hung ydotool/ydotoold would still
# leak a stuck subprocess forever without this.
PASTE_INJECT_TIMEOUT_SECONDS = 3
ACTIVE_APP_TIMEOUT_SECONDS = 0.25


def session_backend(env: dict[str, str] | None = None) -> str:
    """'wayland' or 'x11', based on XDG_SESSION_TYPE (env is injectable for tests)."""
    value = (env if env is not None else os.environ).get("XDG_SESSION_TYPE", "")
    return "wayland" if value == "wayland" else "x11"


def paste_command(
    backend: str,
    which: Callable[[str], str | None],
    shortcut: str = "ctrl+v",
) -> list[str] | None:
    """Argv to inject Ctrl+V for the backend, or None if the tool isn't installed."""
    if backend == "wayland":
        keys = YDOTOOL_CTRL_SHIFT_V if shortcut == "ctrl+shift+v" else YDOTOOL_CTRL_V
        return ["ydotool", "key", *keys] if which("ydotool") else None
    return ["xdotool", "key", shortcut] if which("xdotool") else None


def active_app_class(
    backend: str,
    which: Callable[[str], str | None],
    runner: Callable[..., object],
) -> str | None:
    """Return the active app class before the popup takes focus, or None safely."""
    tool = "kdotool" if backend == "wayland" else "xdotool"
    if not which(tool):
        return None
    command = [tool, "getactivewindow", "getwindowclassname"]
    try:
        result = runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=ACTIVE_APP_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    app_class = str(getattr(result, "stdout", "")).strip().casefold()
    return app_class or None


def parse_app_shortcuts(raw_mapping: str) -> dict[str, str]:
    try:
        mapping = json.loads(raw_mapping)
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(mapping, dict):
        return {}
    return {
        str(key).casefold(): str(value).casefold()
        for key, value in mapping.items()
        if str(value).casefold() in {"ctrl+v", "ctrl+shift+v"}
    }


def format_app_shortcuts(mapping: dict[str, str]) -> str:
    return json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def shortcut_for_app(app_class: str | None, raw_mapping: str) -> str:
    if app_class is None:
        return "ctrl+v"
    return parse_app_shortcuts(raw_mapping).get(app_class.casefold(), "ctrl+v")


def inject_paste(
    backend: str,
    which: Callable[[str], str | None],
    runner: Callable[..., object],
    shortcut: str = "ctrl+v",
) -> bool:
    """Run the paste keystroke injection. Returns False (and logs why) on missing tool/failure."""
    command = paste_command(backend, which, shortcut)
    if command is None:
        logger.warning("paste: no injection tool found for backend=%s", backend)
        return False
    try:
        runner(command, check=True, capture_output=True, timeout=PASTE_INJECT_TIMEOUT_SECONDS)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.warning("paste: injection failed: %s", exc)
        return False
    return True


def notify_paste_unavailable(backend: str, which: Callable[[str], str | None]) -> None:
    """Best-effort desktop notification when auto-paste can't run (tool missing)."""
    if not which("notify-send"):
        return
    tool = "ydotool" if backend == "wayland" else "xdotool"
    subprocess.run(
        ["notify-send", "Keeps", f"Copied to clipboard — install {tool} to enable auto-paste"],
        check=False,
    )
