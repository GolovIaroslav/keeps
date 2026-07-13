import argparse
import os
import shutil
import socket as socket_module
import subprocess
import sys
from pathlib import Path

from keeps import __version__, config, diagnostics
from keeps.store import Store

TOGGLE_MESSAGE = b"toggle"
SHOW_MESSAGE = b"show"


def _socket_path() -> str:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    return str(runtime_dir / "keeps.sock")


def _send_message(socket_path: str, message: bytes) -> bool:
    """Try to deliver a request to an already-running daemon.

    Plain stdlib socket (not QLocalSocket): QLocalServer's Unix-domain socket
    is connectable this way too, and it lets this check run before any Qt
    application object exists.
    """
    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(socket_path)
            sock.sendall(message)
        return True
    except OSError:
        return False


def _make_watcher(store: Store, max_item_mb: float):
    if os.environ.get("WAYLAND_DISPLAY"):
        from keeps.capture.wayland import WaylandWatcher

        return WaylandWatcher(store, max_item_mb=max_item_mb)
    from keeps.capture.x11 import X11Watcher

    return X11Watcher(store, max_item_mb=max_item_mb)


def _make_hotkey(key_sequence: str):
    """KGlobalAccel (any KDE session) first, XGrabKey (plain X11) as fallback.

    Neither works on non-KDE Wayland; callers must still offer `keeps toggle`.
    """
    from keeps.hotkey.kglobalaccel import KGlobalAccelHotkey

    kglobalaccel_hotkey = KGlobalAccelHotkey(key_sequence)
    if kglobalaccel_hotkey.register():
        return kglobalaccel_hotkey

    if not os.environ.get("WAYLAND_DISPLAY"):
        from keeps.hotkey.x11 import XGrabKeyHotkey

        x11_hotkey = XGrabKeyHotkey(key_sequence)
        if x11_hotkey.register():
            return x11_hotkey
    else:
        from keeps.hotkey.portal import PortalGlobalShortcutHotkey

        portal_hotkey = PortalGlobalShortcutHotkey(key_sequence)
        if portal_hotkey.register():
            return portal_hotkey

    return None


def _watch_debug() -> int:
    """Manual smoke-test verb for Ф2: prints each captured clip. Ctrl+C to stop."""
    from PySide6.QtGui import QGuiApplication

    qt_app = QGuiApplication(sys.argv)
    store = Store(Path("/tmp/keeps-watch-debug.db"))

    def on_add(kind: str, mime_data: dict) -> int:
        clip_id = original_add(kind, mime_data)
        print(f"captured: kind={kind} mimes={list(mime_data)} id={clip_id}")
        return clip_id

    original_add = store.add
    store.add = on_add  # type: ignore[method-assign]

    from keeps.capture.base import DEFAULT_MAX_ITEM_MB

    watcher = _make_watcher(store, DEFAULT_MAX_ITEM_MB)
    watcher.start()
    print("watching clipboard, Ctrl+C to stop...")
    return qt_app.exec()


def _popup_debug() -> int:
    """Manual smoke-test verb for Ф3: shows the popup immediately over sample data."""
    from PySide6.QtWidgets import QApplication

    from keeps.ui.popup import PopupWindow
    from keeps.ui.thumbnails import ThumbnailRuntime

    qt_app = QApplication(sys.argv)
    store = Store(Path("/tmp/keeps-popup-debug.db"))
    if not store.all():
        store.add("text", {"text/plain": b"short clip"})
        store.add(
            "text",
            {
                "text/plain": (
                    b"a much longer clip that should wrap across "
                    b"more than one line in the popup delegate, to check "
                    b"3-line wrapping and eliding behaves reasonably"
                )
            },
        )
        store.add(
            "html",
            {"text/plain": b"bold text", "text/html": b"<b>bold text</b>"},
        )
        store.add("files", {"text/uri-list": b"file:///tmp/a.txt\nfile:///tmp/b.txt"})
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d"
            "29dc0000001949444154789c6378e0e0605070019364c02a0a241906a50e00"
            "2ad55a01f9c77e7c0000000049454e44ae426082"
        )
        pinned_id = store.add("image", {"image/png": png})
        store.set_pinned(pinned_id, True)
        for i in range(20):
            store.add("text", {"text/plain": f"filler clip {i}".encode()})

    thumbnail_runtime = ThumbnailRuntime(store)
    popup = PopupWindow(store)
    thumbnail_runtime.thumbnail_ready.connect(popup.on_thumbnail_ready)
    popup.thumbnail_requested.connect(thumbnail_runtime.on_clip_captured)
    thumbnail_runtime.run_backlog_sweep()
    popup.show_popup()
    return qt_app.exec()


def _run_daemon(show_immediately: bool) -> int:
    """The single-instance background process: capture + popup + global hotkey + tray + IPC."""
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from keeps import desktop_entry
    from keeps.ai.runtime import AiRuntime
    from keeps.copy_buffers import CopyBufferController
    from keeps.hotkey.buffers import CopyBufferHotkeyManager
    from keeps.hotkey.clips import ClipGlobalHotkeyManager
    from keeps.ui.popup import PopupWindow
    from keeps.ui.settings import SettingsDialog
    from keeps.ui.thumbnails import ThumbnailRuntime
    from keeps.ui.tray import TrayIcon

    desktop_entry.ensure_installed()

    qt_app = QApplication(sys.argv)
    qt_app.setApplicationName("Keeps")
    qt_app.setApplicationDisplayName("Keeps")
    qt_app.setDesktopFileName("keeps")  # associates KGlobalAccel/tray with our .desktop entry
    qt_app.setQuitOnLastWindowClosed(False)  # popup/settings hide, they don't end the daemon
    # One journal line to diagnose the login-autostart race: a daemon started
    # before the compositor advertises outputs sees only a placeholder screen
    # (see PopupWindow._drop_stale_surface).
    screen_names = [screen.name() or "<unnamed>" for screen in qt_app.screens()]
    print(f"keeps: screens at startup: {screen_names}", file=sys.stderr)

    settings = config.open_settings()
    config.apply_theme(str(config.get(settings, "general/theme")))
    store = Store(
        config.default_db_path(), max_items=int(config.get(settings, "general/max_items"))
    )
    watcher = _make_watcher(store, float(config.get(settings, "general/max_item_mb")))

    ai_runtime = AiRuntime(store, settings)
    thumbnail_runtime = ThumbnailRuntime(store)
    watcher.clip_added.connect(ai_runtime.on_clip_captured)
    watcher.clip_added.connect(thumbnail_runtime.on_clip_captured)
    if ai_runtime.ocr_enabled:
        ai_runtime.run_ocr_backlog_sweep()
    if ai_runtime.rag_text_enabled:
        ai_runtime.run_text_embed_backlog_sweep()

    popup = PopupWindow(store, ai_runtime)
    thumbnail_runtime.thumbnail_ready.connect(popup.on_thumbnail_ready)
    popup.thumbnail_requested.connect(thumbnail_runtime.on_clip_captured)
    watcher.clip_added.connect(popup.on_clip_captured)
    thumbnail_runtime.run_backlog_sweep()
    watcher.start()

    socket_path = _socket_path()
    QLocalServer.removeServer(socket_path)  # drop a stale socket from a crashed instance
    server = QLocalServer()
    if not server.listen(socket_path):
        # continue anyway: the popup still works via the hotkey even without IPC
        print(f"warning: {socket_path}: {server.errorString()}", file=sys.stderr)

    def on_new_connection() -> None:
        connection = server.nextPendingConnection()
        if connection is None:
            return

        def on_ready_read() -> None:
            message = connection.readAll().data()
            if message == TOGGLE_MESSAGE:
                popup.toggle_popup()
            elif message == SHOW_MESSAGE:
                popup.show_popup()

        connection.readyRead.connect(on_ready_read)

    server.newConnection.connect(on_new_connection)

    hotkey = _make_hotkey(str(config.get(settings, "general/hotkey")))
    if hotkey is not None:
        hotkey.triggered.connect(popup.toggle_popup)
        if hasattr(hotkey, "registration_failed"):
            hotkey.registration_failed.connect(
                lambda error: print(
                    f"warning: global shortcut registration failed ({error}); "
                    "bind `keeps toggle` in the compositor settings",
                    file=sys.stderr,
                )
            )
    else:
        print("warning: global hotkey registration failed; use `keeps toggle`", file=sys.stderr)

    runtime_hotkey = {"value": hotkey}

    def apply_runtime_settings() -> None:
        """Apply settings that are owned by the already-running daemon."""
        settings.sync()
        store.set_max_items(int(config.get(settings, "general/max_items")))
        watcher.set_max_item_mb(float(config.get(settings, "general/max_item_mb")))

        desired_sequence = str(config.get(settings, "general/hotkey"))
        current = runtime_hotkey["value"]
        if current is not None and getattr(current, "key_sequence", None) == desired_sequence:
            popup.apply_settings()
            return
        if current is not None:
            current.unregister()
        replacement = _make_hotkey(desired_sequence)
        if replacement is not None:
            replacement.triggered.connect(popup.toggle_popup)
            if hasattr(replacement, "registration_failed"):
                replacement.registration_failed.connect(
                    lambda error: print(
                        f"warning: global shortcut registration failed ({error}); "
                        "bind `keeps toggle` in the compositor settings",
                        file=sys.stderr,
                    )
                )
        else:
            print(
                "warning: global hotkey registration failed after Apply; use `keeps toggle`",
                file=sys.stderr,
            )
        runtime_hotkey["value"] = replacement
        popup.apply_settings()

    popup.set_settings_applier(apply_runtime_settings)

    clip_hotkeys = ClipGlobalHotkeyManager(popup.paste_clip_from_global_hotkey, qt_app)
    popup.set_clip_hotkey_manager(clip_hotkeys)
    clip_hotkeys.restore(store.clips_with_hotkeys(global_only=True))

    copy_buffers = CopyBufferController(store, watcher, settings, qt_app)
    buffer_hotkeys = CopyBufferHotkeyManager(
        copy_buffers.copy_to_buffer, copy_buffers.paste_from_buffer, qt_app
    )
    popup.set_buffer_hotkey_manager(buffer_hotkeys)
    buffer_hotkeys.restore(settings)

    tray = TrayIcon()
    copy_buffers.status_changed.connect(tray.show_message)
    tray.show_requested.connect(popup.show_popup)
    tray.new_clip_requested.connect(popup.new_clip)

    def on_capture_paused_changed(paused: bool) -> None:
        if paused:
            watcher.stop()
        else:
            watcher.start()

    tray.capture_paused_changed.connect(on_capture_paused_changed)

    def on_settings_requested() -> None:
        SettingsDialog(
            ai_runtime,
            store,
            clip_hotkeys=clip_hotkeys,
            buffer_hotkeys=buffer_hotkeys,
            apply_callback=apply_runtime_settings,
        ).exec()
        popup.refresh()

    tray.settings_requested.connect(on_settings_requested)

    def on_quit_requested() -> None:
        clip_hotkeys.deactivate_all()
        buffer_hotkeys.deactivate_all()
        if runtime_hotkey["value"] is not None:
            runtime_hotkey["value"].unregister()
        watcher.stop()
        qt_app.quit()

    tray.quit_requested.connect(on_quit_requested)
    tray.show()

    if show_immediately:
        popup.show_popup()

    return qt_app.exec()


def _status() -> int:
    """`keeps status`: run PLAN.md §8 diagnostics and print ✓/✗ results. No daemon needed."""
    checks = diagnostics.run_all(shutil.which, subprocess.run, Path.exists)
    for check in checks:
        mark = "✓" if check.ok else "✗"
        print(f"{mark} {check.name}: {check.detail}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="keeps")
    parser.add_argument("command", nargs="?", choices=["toggle", "show", "status"], default=None)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--watch-debug", action="store_true", help=argparse.SUPPRESS
    )  # manual Ф2 smoke test
    parser.add_argument(
        "--popup-debug", action="store_true", help=argparse.SUPPRESS
    )  # manual Ф3 smoke test
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return 0

    if args.watch_debug:
        return _watch_debug()

    if args.popup_debug:
        return _popup_debug()

    if args.command == "status":
        return _status()

    socket_path = _socket_path()

    if args.command == "show":
        if _send_message(socket_path, SHOW_MESSAGE):
            return 0
        return _run_daemon(show_immediately=True)

    # `keeps` and `keeps toggle` both wake a live daemon; the difference only
    # matters when none is running yet (PLAN.md §4/§11).
    if _send_message(socket_path, TOGGLE_MESSAGE):
        return 0
    return _run_daemon(show_immediately=(args.command == "toggle"))


if __name__ == "__main__":
    sys.exit(main())
