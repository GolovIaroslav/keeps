# Keeps

A [Ditto](https://github.com/sabrogden/Ditto)-inspired clipboard manager for Linux. Wayland-first, KDE-first, light and fast.

> **Status: pre-alpha.** Early development. Nothing usable yet.

## Why

Existing Linux clipboard managers never quite matched the Ditto experience on Windows. Keeps aims for exactly that:

- **Instant popup** on a global hotkey (`Ctrl+`` by default), on the monitor you're working on.
- **Auto-paste**: pick an item, it's pasted straight into the window you came from. No extra Ctrl+V.
- **Readable history**: multi-line previews (up to 3 lines), image thumbnails, full preview on hover.
- **Search as you type**, Cyrillic-aware, with an optional opt-in semantic mode (multilingual embeddings + OCR for images) — model downloaded only on explicit request, off by default.
- **Predictable ordering**: a used item always jumps to the top. Always.
- **Multi-format**: plain text, rich text (HTML), images, file lists. Paste-as-plain-text with one key.
- History in SQLite (survives reboots), pinned items, tray icon, autostart, GUI settings.
- No telemetry, no accounts, no network (except the optional AI model download).

## Planned stack

Python 3.12 · PySide6 (Qt 6) · SQLite · [wl-clipboard](https://github.com/bugaevc/wl-clipboard) for Wayland clipboard reads · [ydotool](https://github.com/ReimuNotMoe/ydotool) for paste injection · [KGlobalAccel](https://api.kde.org/kglobalaccel.html) for the KDE global hotkey (with fallbacks for other environments).

## License

GPL-3.0. Inspired by Ditto's UX; no code is taken from it — this is an independent implementation for Linux.
