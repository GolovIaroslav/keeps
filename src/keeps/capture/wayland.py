"""Clipboard capture on Wayland via wl-clipboard (wl-paste)."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable

from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtGui import QGuiApplication

from keeps import config
from keeps.capture.base import DEFAULT_MAX_ITEM_MB, SelfSetGuard, build_bundle, should_store
from keeps.store import Store

logger = logging.getLogger(__name__)

# wl-paste --watch runs this command's stdio piped from itself on every clipboard
# change; the command must drain stdin or wl-paste blocks (PLAN.md §11). The
# echoed byte is our only signal — the actual mime types are re-read via a
# separate `wl-paste --list-types` call, since --watch does not report them.
_WATCH_ARGS = ["--watch", "sh", "-c", "cat >/dev/null; echo T"]

# On Wayland the clipboard owner serves content on demand: `wl-paste` blocks
# until the owning client writes the data. If that owner is hung -- or is
# *this very process* (see _on_triggered) -- an untimed read would block the
# Qt main loop forever, and with it every other app's paste (they all wait on
# the same owner). Observed live 2026-07-10 as a system-wide clipboard freeze.
WL_PASTE_TIMEOUT_SECONDS = 3


class WaylandWatcher(QObject):
    # Emitted right after a clip is inserted/touched -- the only clean hook
    # for post-capture processing (OCR scheduling) without polling.
    clip_added = Signal(int, str)  # (clip_id, kind)

    def __init__(
        self,
        store: Store,
        max_item_mb: float = DEFAULT_MAX_ITEM_MB,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._max_item_mb = max_item_mb
        self.guard = SelfSetGuard()
        self._process = QProcess(self)
        self._process.setProgram("wl-paste")
        self._process.setArguments(_WATCH_ARGS)
        self._process.readyReadStandardOutput.connect(self._on_triggered)
        self._buffer_capture: Callable[[str, dict[str, bytes]], None] | None = None

    def start(self) -> None:
        self._process.start()

    def set_max_item_mb(self, max_item_mb: float) -> None:
        self._max_item_mb = max_item_mb

    def stop(self) -> None:
        self._process.terminate()
        self._process.waitForFinished(1000)

    def capture_next_for_buffer(self, callback: Callable[[str, dict[str, bytes]], None]) -> None:
        """Consume the next external supported selection for a copy-buffer operation.

        The callback runs before Store.add(), so temporary Ctrl+C contents do
        not enter normal history. Only the controller arms this one-shot hook.
        """
        self._buffer_capture = callback

    def cancel_buffer_capture(self) -> None:
        self._buffer_capture = None

    def _on_triggered(self) -> None:
        self._process.readAllStandardOutput()
        # Our own clipboard write (popup paste/copy) also fires --watch. Reading
        # it back would deadlock: wl-paste asks the owner (us) for the data, but
        # the owner's main thread is the one blocked inside that very wl-paste
        # call. The clip is already in the store (touch()ed on activation), so
        # there is nothing to capture anyway.
        if QGuiApplication.clipboard().ownsClipboard():
            return
        result = self._capture_bundle()
        if result is None:
            return
        callback = self._buffer_capture
        if callback is not None:
            self._buffer_capture = None
            callback(*result)
            return
        if self.guard.consume_skip():
            return
        self._store_bundle(*result)

    def _capture_bundle(self) -> tuple[str, dict[str, bytes]] | None:
        available = self._list_types()
        if available is None:
            return None
        settings = config.open_settings()
        try:
            result = build_bundle(
                available,
                self._read_mime,
                self._max_item_mb,
                store_all_formats=bool(config.get(settings, "capture/store_all_formats")),
            )
        except subprocess.TimeoutExpired:
            logger.warning("wl-paste read timed out; clipboard owner unresponsive, skipping clip")
            return None
        return result

    def _store_bundle(self, kind: str, mime_data: dict[str, bytes]) -> None:
        if not self._should_store(kind):
            return
        clip_id = self._store.add(kind, mime_data)
        self.clip_added.emit(clip_id, kind)

    @staticmethod
    def _should_store(kind: str) -> bool:
        settings = config.open_settings()
        return should_store(
            kind,
            config.get(settings, "capture/store_html"),
            config.get(settings, "capture/store_images"),
            config.get(settings, "capture/store_files"),
        )

    @staticmethod
    def _list_types() -> set[str] | None:
        try:
            proc = subprocess.run(
                ["wl-paste", "--list-types"],
                capture_output=True,
                text=True,
                check=False,
                timeout=WL_PASTE_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            logger.warning("wl-paste --list-types timed out, skipping clip")
            return None
        if proc.returncode != 0:
            logger.debug("wl-paste --list-types failed: %s", proc.stderr.strip())
            return None
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    @staticmethod
    def _read_mime(mime: str) -> bytes:
        # TimeoutExpired propagates to _capture, which aborts the whole bundle.
        proc = subprocess.run(
            ["wl-paste", "-n", "--type", mime],
            capture_output=True,
            check=False,
            timeout=WL_PASTE_TIMEOUT_SECONDS,
        )
        return proc.stdout
