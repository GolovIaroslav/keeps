import argparse
import os
import sys
from pathlib import Path

from keeps import __version__
from keeps.store import Store


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

    if os.environ.get("WAYLAND_DISPLAY"):
        from keeps.capture.wayland import WaylandWatcher

        watcher = WaylandWatcher(store)
    else:
        from keeps.capture.x11 import X11Watcher

        watcher = X11Watcher(store)

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


def main() -> int:
    parser = argparse.ArgumentParser(prog="keeps")
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

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
