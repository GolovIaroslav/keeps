"""Qt/KGlobalAccel wiring for per-clip global hotkeys (PLAN.md Ф20)."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject

from keeps.hotkey.clip_registry import ClipHotkeyAction, ClipHotkeyRegistry
from keeps.hotkey.kglobalaccel import KGlobalAccelHotkey
from keeps.store import Clip

logger = logging.getLogger(__name__)


class ClipGlobalHotkeyManager(QObject):
    """Own KGlobalAccel objects and dispatch their stable clip-id actions."""

    def __init__(self, on_triggered: Callable[[int], None], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._known_global_clip_ids: set[int] = set()
        self._registry = ClipHotkeyRegistry(on_triggered, self._create_action)

    @property
    def count(self) -> int:
        return self._registry.count

    def _create_action(self, clip_id: int, sequence: str) -> KGlobalAccelHotkey:
        return KGlobalAccelHotkey(
            sequence,
            self,
            action_unique=f"clip-{clip_id}",
            action_friendly=self.tr("Paste clip {clip_id}").format(clip_id=clip_id),
        )

    @staticmethod
    def _delete_later(action: ClipHotkeyAction | None) -> None:
        if action is not None:
            getattr(action, "deleteLater", lambda: None)()

    def register(self, clip_id: int, sequence: str) -> str | None:
        error = self._registry.register(clip_id, sequence)
        if error is None:
            self._known_global_clip_ids.add(clip_id)
        return error

    def restore(self, clips: Iterable[Clip]) -> None:
        """Re-register persisted global assignments after daemon startup."""
        for clip in clips:
            if not clip.hotkey or not clip.hotkey_global:
                continue
            self._known_global_clip_ids.add(clip.id)
            error = self._registry.register(clip.id, clip.hotkey)
            if error:
                logger.warning("clip hotkey %s was not restored: %s", clip.id, error)

    def unregister(self, clip_id: int) -> None:
        """Permanently erase even an action that could not be restored."""
        action = self._registry.remove(clip_id)
        if action is None:
            orphan = self._create_action(clip_id, "")
            orphan.unregister(remove=True)
            action = orphan
        self._known_global_clip_ids.discard(clip_id)
        self._delete_later(action)

    def prune(self, existing_clip_ids: set[int]) -> None:
        """Erase actions for clips removed by trim or another Store caller."""
        for clip_id in self._known_global_clip_ids - existing_clip_ids:
            self.unregister(clip_id)

    def deactivate_all(self) -> None:
        self._registry.deactivate_all()
