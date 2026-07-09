# Keeps

A [Ditto](https://github.com/sabrogden/Ditto)-inspired clipboard manager for Linux. Wayland-first, KDE-first, light and fast.

> **Status: MVP in progress.** Core clipboard history, popup, auto-paste, hotkey, tray, and settings all work day-to-day on KDE Plasma Wayland. Not yet packaged or tagged as a release — expect rough edges.

## Why

Existing Linux clipboard managers never quite matched the Ditto experience on Windows. Keeps aims for exactly that:

- **Instant popup** on a global hotkey (`Ctrl+`` by default), on the monitor you're working on.
- **Auto-paste**: pick an item, it's pasted straight into the window you came from. No extra Ctrl+V.
- **Readable history**: multi-line previews (up to 3 lines), image thumbnails, full preview on hover.
- **Search as you type**, Cyrillic-aware.
- **Predictable ordering**: a used item always jumps to the top. Always.
- **Multi-format**: plain text, rich text (HTML), images, file lists. Paste-as-plain-text with one key.
- History in SQLite (survives reboots), pinned items, tray icon, autostart, GUI settings.
- No telemetry, no accounts, no network calls (an opt-in AI search mode with a one-time model download is planned).

## Install (Arch Linux)

```sh
sudo pacman -S pyside6 wl-clipboard ydotool tesseract tesseract-data-eng tesseract-data-rus
git clone https://github.com/GolovIaroslav/keeps
cd keeps
uv sync
uv run keeps
```

`ydotool` and `tesseract` are optional: without `ydotool`, Keeps still copies to the clipboard but can't auto-paste; `tesseract` is only needed for the future OCR feature. Run `keeps status` to check what's available.

## Usage

- `keeps` — start the background daemon (or toggle the popup if it's already running).
- `keeps toggle` — same, but always show the popup on first launch.
- `keeps show` — show the popup without toggling it closed.
- `keeps status` — run diagnostics (wl-paste, ydotool, kglobalaccel, session type, Klipper, tesseract).

The tray icon has Show / Pause capture / Settings / Quit. Settings (`general`/`capture`/`ai` tabs) live at `~/.config/keeps/keeps.ini`.

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
| `Esc` / focus loss | hide popup |

On KDE, the global hotkey is registered via KGlobalAccel; on plain X11 desktops it falls back to a direct XGrabKey. Neither works on non-KDE Wayland compositors (e.g. GNOME, Sway) — bind `keeps toggle` to a key in your compositor's own shortcut settings instead.

## Stack

Python 3.12 · PySide6 (Qt 6) · SQLite · [wl-clipboard](https://github.com/bugaevc/wl-clipboard) for Wayland clipboard reads · [ydotool](https://github.com/ReimuNotMoe/ydotool)/xdotool for paste injection · [KGlobalAccel](https://api.kde.org/kglobalaccel.html) for the KDE global hotkey.

## License

GPL-3.0. Inspired by Ditto's UX; no code is taken from it — this is an independent implementation for Linux.
