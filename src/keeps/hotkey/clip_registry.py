"""Pure policy for dynamic per-clip global shortcut actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

MAX_GLOBAL_CLIP_HOTKEYS = 20


class _Trigger(Protocol):
    def connect(self, callback: Callable[[], None]) -> object: ...


class ClipHotkeyAction(Protocol):
    key_sequence: str
    last_error: str
    triggered: _Trigger

    def register(self) -> bool: ...

    def set_key_sequence(self, key_sequence: str) -> bool: ...

    def unregister(self, *, remove: bool = False) -> None: ...


class ClipHotkeyRegistry:
    """Register up to 20 stable actions and route each back to its clip id."""

    def __init__(
        self,
        on_triggered: Callable[[int], None],
        action_factory: Callable[[int, str], ClipHotkeyAction],
    ) -> None:
        self._on_triggered = on_triggered
        self._action_factory = action_factory
        self._registered: dict[int, ClipHotkeyAction] = {}

    @staticmethod
    def _discard(action: ClipHotkeyAction) -> None:
        """Release a partly registered action before dropping its reference."""
        action.unregister(remove=True)
        getattr(action, "deleteLater", lambda: None)()

    @property
    def count(self) -> int:
        return len(self._registered)

    def register(self, clip_id: int, sequence: str) -> str | None:
        """Register or replace a clip action; return a stable error code."""
        existing = self._registered.get(clip_id)
        if existing is not None:
            if existing.key_sequence == sequence:
                return None
            if existing.set_key_sequence(sequence):
                return None
            return existing.last_error or "registration-failed"

        if self.count >= MAX_GLOBAL_CLIP_HOTKEYS:
            return "limit"

        action = self._action_factory(clip_id, sequence)
        if not action.register():
            self._discard(action)
            return action.last_error or "registration-failed"
        action.triggered.connect(lambda clip_id=clip_id: self._on_triggered(clip_id))
        self._registered[clip_id] = action
        return None

    def remove(self, clip_id: int) -> ClipHotkeyAction | None:
        """Forget and permanently unregister an action if it was active."""
        action = self._registered.pop(clip_id, None)
        if action is not None:
            action.unregister(remove=True)
        return action

    def prune(self, existing_clip_ids: set[int]) -> list[ClipHotkeyAction]:
        """Remove all runtime actions whose clip has disappeared from Store."""
        removed = []
        for clip_id in set(self._registered) - existing_clip_ids:
            action = self.remove(clip_id)
            if action is not None:
                removed.append(action)
        return removed

    def deactivate_all(self) -> None:
        """Release session grabs without changing their durable assignments."""
        for action in self._registered.values():
            action.unregister()
