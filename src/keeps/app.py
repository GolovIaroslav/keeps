import argparse
import os
import socket as socket_module
import sys
from pathlib import Path

from keeps import __version__
from keeps.store import Store

TOGGLE_MESSAGE = b"toggle"


def _socket_path() -> str:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    return str(runtime_dir / "keeps.sock")


def _default_db_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    directory = data_home / "keeps"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "keeps.db"


def _send_toggle(socket_path: str) -> bool:
    """Try to deliver a toggle request to an already-running daemon.

    Plain stdlib socket (not QLocalSocket): QLocalServer's Unix-domain socket
    is connectable this way too, and it lets this check run before any Qt
    application object exists.
    """
    try:
        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            sock.connect(socket_path)
            sock.sendall(TOGGLE_MESSAGE)
        return True
    except OSError:
        return False


def _make_watcher(store: Store):
    if os.environ.get("WAYLAND_DISPLAY"):
        from keeps.capture.wayland import WaylandWatcher

        return WaylandWatcher(store)
    from keeps.capture.x11 import X11Watcher

    return X11Watcher(store)


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

    watcher = _make_watcher(store)
    watcher.start()
    print("watching clipboard, Ctrl+C to stop...")
    return qt_app.exec()


def _popup_debug() -> int:
    """Manual smoke-test verb for Ф3: shows the popup immediately over sample data."""
    from PySide6.QtWidgets import QApplication

    from keeps.ui.popup import PopupWindow

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

    popup = PopupWindow(store)
    popup.show_popup()
    return qt_app.exec()


def _run_daemon(show_immediately: bool) -> int:
    """The single-instance background process: capture + popup + global hotkey + IPC.

    `keeps toggle`/hotkey fallback wiring (single instance, tray, autostart)
    beyond this is the rest of Ф5, deferred to a later session.
    """
    from PySide6.QtNetwork import QLocalServer
    from PySide6.QtWidgets import QApplication

    from keeps.hotkey.kglobalaccel import KGlobalAccelHotkey
    from keeps.ui.popup import PopupWindow

    qt_app = QApplication(sys.argv)
    store = Store(_default_db_path())
    watcher = _make_watcher(store)
    watcher.start()

    popup = PopupWindow(store)

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
            if connection.readAll().data() == TOGGLE_MESSAGE:
                popup.toggle_popup()

        connection.readyRead.connect(on_ready_read)

    server.newConnection.connect(on_new_connection)

    hotkey = KGlobalAccelHotkey("Ctrl+`")
    if hotkey.register():
        hotkey.triggered.connect(popup.toggle_popup)
    else:
        print("warning: global hotkey registration failed; use `keeps toggle`", file=sys.stderr)

    if show_immediately:
        popup.show_popup()

    return qt_app.exec()


def main() -> int:
    parser = argparse.ArgumentParser(prog="keeps")
    parser.add_argument("command", nargs="?", choices=["toggle"], default=None)
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

    # `keeps` and `keeps toggle` both wake a live daemon; the difference only
    # matters when none is running yet (PLAN.md §4/§11).
    if _send_toggle(_socket_path()):
        return 0
    return _run_daemon(show_immediately=(args.command == "toggle"))


if __name__ == "__main__":
    sys.exit(main())
