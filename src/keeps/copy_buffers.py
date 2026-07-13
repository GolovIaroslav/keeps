"""Persistent copy-buffer transactions that preserve ordinary clipboard contents."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from keeps import config, paste
from keeps.clipboard import make_mime_data, restore_mime_data, snapshot_mime_data
from keeps.store import Store

logger = logging.getLogger(__name__)

COPY_CAPTURE_TIMEOUT_MS = 1_000
POST_PASTE_GRACE_MS = 500
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class BufferCaptureWatcher(Protocol):
    def capture_next_for_buffer(
        self, callback: Callable[[str, dict[str, bytes]], None]
    ) -> None: ...

    def cancel_buffer_capture(self) -> None: ...


@dataclass
class _Operation:
    kind: str
    slot: int
    snapshot: dict[str, bytes]
    paste_shortcut: str | None = None


class _InjectionResult(QObject):
    completed = Signal(bool)


class _InjectionTask(QRunnable):
    def __init__(self, action: Callable[[], bool], result: _InjectionResult) -> None:
        super().__init__()
        self._action = action
        self._result = result

    def run(self) -> None:
        self._result.completed.emit(self._action())


class CopyBufferController(QObject):
    """Serialize temporary clipboard ownership for the three global buffers.

    A Wayland paste target requests clipboard data asynchronously. Restoring
    the prior owner after a fixed post-injection grace is therefore best-effort
    by design; ownership is checked immediately before every restoration.
    """

    status_changed = Signal(str)

    def __init__(
        self,
        store: Store,
        watcher: BufferCaptureWatcher,
        settings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._watcher = watcher
        self._settings = settings
        self._operation: _Operation | None = None
        self._copy_timeout = QTimer(self)
        self._copy_timeout.setSingleShot(True)
        self._copy_timeout.timeout.connect(self._on_copy_timeout)
        self._paste_delay = QTimer(self)
        self._paste_delay.setSingleShot(True)
        self._paste_delay.timeout.connect(self._start_paste_injection)
        self._paste_grace = QTimer(self)
        self._paste_grace.setSingleShot(True)
        self._paste_grace.timeout.connect(self._restore_after_paste)

    @property
    def busy(self) -> bool:
        return self._operation is not None

    def copy_to_buffer(self, slot: int) -> bool:
        """Inject Ctrl+C, intercept its next capture event, then restore the clipboard."""
        if self.busy:
            self._status("Copy buffer operation is already in progress.")
            return False
        snapshot = self._snapshot()
        if snapshot is None:
            return False
        self._operation = _Operation("copy", slot, snapshot)
        self._watcher.capture_next_for_buffer(self._on_buffer_captured)
        self._copy_timeout.start(COPY_CAPTURE_TIMEOUT_MS)
        self._run_injection(
            lambda: paste.inject_copy(paste.session_backend(), shutil.which, subprocess.run)
        )
        return True

    def paste_from_buffer(self, slot: int) -> bool:
        """Temporarily publish a buffer, inject paste, then best-effort restore."""
        if self.busy:
            self._status("Copy buffer operation is already in progress.")
            return False
        buffer = self._store.get_copy_buffer(slot)
        if buffer is None:
            self._status(f"Copy buffer {slot} is empty.")
            return False
        snapshot = self._snapshot()
        if snapshot is None:
            return False
        backend = paste.session_backend()
        target = paste.active_app_class(backend, shutil.which, subprocess.run)
        shortcut = paste.shortcut_for_app(
            target, str(config.get(self._settings, "paste/app_shortcuts"))
        )
        self._operation = _Operation("paste", slot, snapshot, shortcut)
        QGuiApplication.clipboard().setMimeData(make_mime_data(buffer.mime_data))
        self._paste_delay.start(int(config.get(self._settings, "paste/delay_ms")))
        return True

    def _snapshot(self) -> dict[str, bytes] | None:
        mime_data = QGuiApplication.clipboard().mimeData()
        if mime_data is None:
            self._status("Could not read the current clipboard; buffer unchanged.")
            return None
        snapshot = snapshot_mime_data(mime_data, max_bytes=MAX_SNAPSHOT_BYTES)
        if snapshot is None:
            self._status("Current clipboard is too large to preserve safely; buffer unchanged.")
        return snapshot

    def _run_injection(self, action: Callable[[], bool]) -> None:
        result = _InjectionResult(self)
        result.completed.connect(self._on_injection_finished)
        QThreadPool.globalInstance().start(_InjectionTask(action, result))

    def _on_buffer_captured(self, kind: str, mime_data: dict[str, bytes]) -> None:
        operation = self._operation
        if operation is None or operation.kind != "copy":
            return
        self._copy_timeout.stop()
        self._store.set_copy_buffer(operation.slot, kind, mime_data)
        # The copied selection is known to be the transaction's just-captured
        # source, so restoring immediately is required to preserve the prior
        # ordinary clipboard. The delayed Paste path intentionally does not
        # force this write because a user may have copied newer content.
        self._restore_snapshot(operation.snapshot, force=True)
        self._operation = None
        self._status(f"Saved to copy buffer {operation.slot}.")

    def _on_copy_timeout(self) -> None:
        operation = self._operation
        if operation is None or operation.kind != "copy":
            return
        self._watcher.cancel_buffer_capture()
        self._restore_snapshot(operation.snapshot)
        self._operation = None
        self._status("Copy buffer timed out; buffer unchanged.")

    def _start_paste_injection(self) -> None:
        operation = self._operation
        if operation is None or operation.kind != "paste":
            return
        # A newer clipboard owner means Ctrl+V would paste unrelated content.
        if not QGuiApplication.clipboard().ownsClipboard():
            self._operation = None
            self._status("Clipboard changed before paste; buffer paste cancelled.")
            return
        backend = paste.session_backend()
        shortcut = operation.paste_shortcut or "ctrl+v"
        self._run_injection(
            lambda: paste.inject_paste(backend, shutil.which, subprocess.run, shortcut)
        )

    def _on_injection_finished(self, success: bool) -> None:
        operation = self._operation
        if operation is None:
            return
        if operation.kind == "copy":
            if not success:
                self._copy_timeout.stop()
                self._watcher.cancel_buffer_capture()
                self._restore_snapshot(operation.snapshot)
                self._operation = None
                self._status("Copy buffer failed; buffer unchanged.")
            return
        if success:
            self._paste_grace.start(POST_PASTE_GRACE_MS)
            return
        self._restore_snapshot(operation.snapshot)
        self._operation = None
        self._status("Copy buffer paste failed; clipboard restored.")

    def _restore_after_paste(self) -> None:
        operation = self._operation
        if operation is None or operation.kind != "paste":
            return
        self._restore_snapshot(operation.snapshot)
        self._operation = None

    @staticmethod
    def _restore_snapshot(snapshot: dict[str, bytes], *, force: bool = False) -> None:
        clipboard = QGuiApplication.clipboard()
        if force or clipboard.ownsClipboard():
            clipboard.setMimeData(restore_mime_data(snapshot))

    def _status(self, message: str) -> None:
        logger.info("copy buffer: %s", message)
        self.status_changed.emit(self.tr(message))
