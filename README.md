# Keeps

A [Ditto](https://github.com/sabrogden/Ditto)-inspired clipboard manager for Linux. Wayland-first, KDE-first, light and fast.

> **Status: v0.2.0.** Core clipboard history, popup, auto-paste, hotkey, tray, settings, and opt-in AI search (OCR + semantic text search) all work day-to-day on KDE Plasma Wayland. Install from source, the AUR recipe in `packaging/aur/`, or grab the AppImage from [Releases](https://github.com/GolovIaroslav/keeps/releases).

## Why

Existing Linux clipboard managers never quite matched the Ditto experience on Windows. Keeps aims for exactly that:

- **Instant popup** on a global hotkey (`Ctrl+`` by default), on the monitor you're working on.
- **Auto-paste**: pick an item, it's pasted straight into the window you came from. No extra Ctrl+V.
- **Readable history**: multi-line previews (up to 3 lines), image thumbnails, full preview on hover.
- **Search as you type**, Cyrillic-aware.
- **Predictable ordering**: a used item always jumps to the top. Always.
- **Multi-format**: plain text, rich text (HTML), images, file lists. Optionally preserve extra source MIME formats too. Paste-as-plain-text with one key.
- History in SQLite (survives reboots), pinned items, tray icon, autostart, GUI settings.
- No telemetry, no accounts, no network calls except an explicit, opt-in AI model download (semantic text search + OCR).

## Install

### AppImage (any distro)

Download the latest `keeps-*-x86_64.AppImage` from [Releases](https://github.com/GolovIaroslav/keeps/releases), `chmod +x` it, and run. Still needs `wl-clipboard` (Wayland) and, optionally, `ydotool` for auto-paste from the host system — see `keeps status` after first run.

### From source (Arch Linux)

```sh
sudo pacman -S pyside6 wl-clipboard ydotool
git clone https://github.com/GolovIaroslav/keeps
cd keeps
uv sync
uv run keeps
```

`ydotool` is optional: without it, Keeps still copies to the clipboard but can't auto-paste. For the opt-in AI search (OCR + semantic text search), also run `uv sync --extra ai` — model weights are downloaded separately, on request, from the app's own Model management settings. Run `keeps status` to check what's available.

## Usage

- `keeps` — start the background daemon (or toggle the popup if it's already running).
- `keeps toggle` — same, but always show the popup on first launch.
- `keeps show` — show the popup without toggling it closed.
- `keeps status` — run diagnostics (wl-paste, ydotool, kglobalaccel, session type, Klipper, AI models).

The tray icon has Show / New clip / Pause capture / Settings / Quit. Right-click selected clips to compare two text clips or export them; use the popup title bar to create or import clips. Settings (`general`/`capture`/`ai` tabs) live at `~/.config/keeps/keeps.ini`.

### Popup keymap

| Key | Action |
|---|---|
| `Ctrl+`` (global) | show/hide popup |
| type | filter list live |
| `↑/↓`, `PgUp/PgDn` | navigate |
| `Enter` / double-click | paste selected item (any format) |
| `Shift+Enter` | paste as plain text |
| `Ctrl+C` | copy only, no paste |
| `Del` | delete item |
| `Ctrl+E` | edit in an external editor |
| `Ctrl+P` | pin/unpin |
| `Ctrl+1..9` | paste the Nth visible item |
| `Ctrl+M` | cycle search mode: blended → keywords → meaning (only when AI text search is enabled) |
| `Esc` / focus loss | hide popup |

On KDE, the global hotkey is registered via KGlobalAccel; on plain X11 desktops it falls back to a direct XGrabKey. On other Wayland compositors, Keeps tries the XDG GlobalShortcuts portal and falls back to a compositor-bound `keeps toggle` shortcut if the portal is unavailable or declined.

### Desktop environments

- **KDE Plasma:** Keeps registers its global shortcut through KGlobalAccel. If the key is already assigned, remove the old assignment in System Settings, then assign it to Keeps.
- **GNOME, Sway, Hyprland, and other Wayland compositors:** the XDG GlobalShortcuts portal is tried first. If your portal backend does not provide this interface or the consent dialog is declined, add a custom keyboard shortcut that runs `keeps toggle`.
- **X11 desktops:** Keeps tries a native XGrabKey registration. If that is unavailable, use the same `keeps toggle` custom shortcut.

The popup is deliberately placed by the compositor on Wayland; there is no portable API for reading the pointer position of another application. `keeps status` reports missing `wl-clipboard`, `ydotool`, or hotkey services with a suggested fix.

## Stack

Python 3.12 · PySide6 (Qt 6) · SQLite · [wl-clipboard](https://github.com/bugaevc/wl-clipboard) for Wayland clipboard reads · [ydotool](https://github.com/ReimuNotMoe/ydotool)/xdotool for paste injection · [KGlobalAccel](https://api.kde.org/kglobalaccel.html) for the KDE global hotkey.

## License

GPL-3.0. Inspired by Ditto's UX; no code is taken from it — this is an independent implementation for Linux.
