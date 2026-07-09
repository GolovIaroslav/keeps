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


def main() -> int:
    parser = argparse.ArgumentParser(prog="keeps")
    parser.add_argument("--version", action="store_true", help="print version and exit")
    parser.add_argument(
        "--watch-debug", action="store_true", help=argparse.SUPPRESS
    )  # manual Ф2 smoke test
    args = parser.parse_args()

    if args.version:
        print(__version__)
        return 0

    if args.watch_debug:
        return _watch_debug()

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
