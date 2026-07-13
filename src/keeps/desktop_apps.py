"""Small freedesktop application discovery and command-line helpers."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DesktopApplication:
    """A visible application entry that can receive a temporary file."""

    desktop_id: str
    name: str
    exec_line: str
    icon: str = ""


def _data_directories(
    data_home: Path | None = None, data_dirs: Iterable[Path] | None = None
) -> tuple[Path, ...]:
    home = data_home or Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    if data_dirs is None:
        raw_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
        data_dirs = (Path(value) for value in raw_dirs.split(":") if value)
    return (home, *data_dirs)


def _desktop_entry(path: Path, desktop_id: str) -> DesktopApplication | None:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines:
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values.setdefault(key, value.strip())
    if (
        values.get("Type") != "Application"
        or values.get("NoDisplay", "false").casefold() == "true"
        or values.get("Hidden", "false").casefold() == "true"
        or not values.get("Name")
        or not values.get("Exec")
    ):
        return None
    return DesktopApplication(desktop_id, values["Name"], values["Exec"], values.get("Icon", ""))


def installed_applications(
    *, data_home: Path | None = None, data_dirs: Iterable[Path] | None = None
) -> list[DesktopApplication]:
    """Return visible installed GUI applications, with user entries winning.

    Desktop IDs are de-duplicated in freedesktop search order: the user's data
    directory is searched first, followed by system data directories.
    """
    applications: dict[str, DesktopApplication] = {}
    for directory in _data_directories(data_home, data_dirs):
        applications_dir = directory / "applications"
        try:
            paths = sorted(applications_dir.rglob("*.desktop"))
        except OSError:
            continue
        for path in paths:
            desktop_id = path.relative_to(applications_dir).as_posix()
            if desktop_id in applications:
                continue
            app = _desktop_entry(path, desktop_id)
            if app is not None:
                applications[desktop_id] = app
    return sorted(applications.values(), key=lambda app: (app.name.casefold(), app.desktop_id))


def command_for_files(command_line: str, paths: Iterable[Path | str]) -> list[str]:
    """Turn a desktop ``Exec=`` or user command into argv for file paths."""
    files = [str(path) for path in paths]
    command_line = command_line.strip()
    if not command_line:
        return ["xdg-open", *files]
    try:
        tokens = shlex.split(command_line)
    except ValueError as exc:
        raise ValueError("Invalid application command") from exc

    result: list[str] = []
    used_file_field = False
    for token in tokens:
        if token in {"%f", "%u", "%F", "%U"}:
            result.extend(files)
            used_file_field = True
            continue
        if token in {"%i", "%c", "%k"}:
            continue
        token = token.replace("%%", "%")
        for field in ("%f", "%u", "%F", "%U"):
            if field in token:
                token = token.replace(field, files[0] if files else "")
                used_file_field = True
        for field in ("%i", "%c", "%k"):
            token = token.replace(field, "")
        if token:
            result.append(token)
    if not used_file_field:
        result.extend(files)
    return result
