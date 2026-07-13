"""Stable KGlobalAccel actions for the three persistent copy buffers."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject

from keeps.hotkey.kglobalaccel import KGlobalAccelHotkey


class CopyBufferHotkeyManager(QObject):
    """Own the six configurable global Copy/Paste action registrations."""

    def __init__(
        self,
        on_copy: Callable[[int], None],
        on_paste: Callable[[int], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._on_copy = on_copy
        self._on_paste = on_paste
        self._actions: dict[tuple[int, str], KGlobalAccelHotkey] = {}

    def configure(self, slot: int, operation: str, sequence: str) -> str | None:
        """Set one assignment; blank means remove it and returns no error."""
        if slot not in (1, 2, 3) or operation not in {"copy", "paste"}:
            raise ValueError("invalid copy-buffer hotkey action")
        key = (slot, operation)
        sequence = sequence.strip()
        existing = self._actions.get(key)
        if not sequence:
            if existing is not None:
                existing.unregister(remove=True)
                existing.deleteLater()
                del self._actions[key]
            return None
        if existing is not None:
            if existing.set_key_sequence(sequence):
                return None
            return existing.last_error or "registration-failed"
        action = KGlobalAccelHotkey(
            sequence,
            self,
            action_unique=f"buffer-{slot}-{operation}",
            action_friendly=self.tr("{operation} copy buffer {slot}").format(
                operation=operation.title(), slot=slot
            ),
        )
        if not action.register():
            action.unregister(remove=True)
            action.deleteLater()
            return action.last_error or "registration-failed"
        action.triggered.connect(
            lambda slot=slot, operation=operation: self._dispatch(slot, operation)
        )
        self._actions[key] = action
        return None

    def restore(self, settings) -> None:
        for slot in (1, 2, 3):
            for operation in ("copy", "paste"):
                key = f"buffers/{slot}/{operation}_hotkey"
                self.configure(slot, operation, str(settings.value(key, "")))

    def deactivate_all(self) -> None:
        for action in self._actions.values():
            action.unregister()

    def _dispatch(self, slot: int, operation: str) -> None:
        if operation == "copy":
            self._on_copy(slot)
        else:
            self._on_paste(slot)
