"""Clipboard capture on Wayland via wl-clipboard (wl-paste)."""

from __future__ import annotations

import logging
import subprocess

from PySide6.QtCore import QObject, QProcess, Signal

from keeps import config
from keeps.capture.base import DEFAULT_MAX_ITEM_MB, SelfSetGuard, build_bundle, should_store
from keeps.store import Store

logger = logging.getLogger(__name__)

# wl-paste --watch runs this command's stdio piped from itself on every clipboard
# change; the command must drain stdin or wl-paste blocks (PLAN.md §11). The
# echoed byte is our only signal — the actual mime types are re-read via a
# separate `wl-paste --list-types` call, since --watch does not report them.
_WATCH_ARGS = ["--watch", "sh", "-c", "cat >/dev/null; echo T"]


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

    def start(self) -> None:
        self._process.start()

    def stop(self) -> None:
        self._process.terminate()
        self._process.waitForFinished(1000)

    def _on_triggered(self) -> None:
        self._process.readAllStandardOutput()
        if self.guard.consume_skip():
            return
        self._capture()

    def _capture(self) -> None:
        available = self._list_types()
        if available is None:
            return
        result = build_bundle(available, self._read_mime, self._max_item_mb)
        if result is None:
            return
        kind, mime_data = result
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
        proc = subprocess.run(
            ["wl-paste", "--list-types"], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            logger.debug("wl-paste --list-types failed: %s", proc.stderr.strip())
            return None
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    @staticmethod
    def _read_mime(mime: str) -> bytes:
        proc = subprocess.run(
            ["wl-paste", "-n", "--type", mime], capture_output=True, check=False
        )
        return proc.stdout
