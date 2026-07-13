from keeps.hotkey.clip_registry import MAX_GLOBAL_CLIP_HOTKEYS, ClipHotkeyRegistry


class FakeTrigger:
    def __init__(self):
        self._callback = None

    def connect(self, callback):
        self._callback = callback

    def emit(self):
        assert self._callback is not None
        self._callback()


class FakeHotkey:
    def __init__(self, sequence, action_unique, action_friendly):
        self.key_sequence = sequence
        self.action_unique = action_unique
        self.action_friendly = action_friendly
        self.last_error = ""
        self.triggered = FakeTrigger()
        self.remove_calls = []

    def register(self):
        if self.key_sequence == "Ctrl+Taken":
            self.last_error = "conflict"
            return False
        return True

    def set_key_sequence(self, sequence):
        if sequence == "Ctrl+Taken":
            self.last_error = "conflict"
            return False
        self.key_sequence = sequence
        return True

    def unregister(self, *, remove=False):
        self.remove_calls.append(remove)


def test_clip_global_hotkey_dispatches_its_stable_clip_id():
    triggered = []
    actions = {}

    def create(clip_id, sequence):
        action = FakeHotkey(sequence, f"clip-{clip_id}", f"Paste clip {clip_id}")
        actions[clip_id] = action
        return action

    registry = ClipHotkeyRegistry(triggered.append, create)
    assert registry.register(42, "Meta+G") is None
    actions[42].triggered.emit()

    assert triggered == [42]
    assert actions[42].action_unique == "clip-42"
    assert actions[42].action_friendly == "Paste clip 42"


def test_clip_global_hotkey_rejects_conflict_without_dropping_old_binding():
    actions = {}

    def create(clip_id, sequence):
        action = FakeHotkey(sequence, f"clip-{clip_id}", f"Paste clip {clip_id}")
        actions[clip_id] = action
        return action

    registry = ClipHotkeyRegistry(lambda _clip_id: None, create)
    assert registry.register(42, "Meta+G") is None

    assert registry.register(42, "Ctrl+Taken") == "conflict"
    assert actions[42].key_sequence == "Meta+G"


def test_failed_new_clip_global_hotkey_is_unregistered_before_discard():
    actions = {}

    def create(clip_id, sequence):
        action = FakeHotkey(sequence, f"clip-{clip_id}", f"Paste clip {clip_id}")
        actions[clip_id] = action
        return action

    registry = ClipHotkeyRegistry(lambda _clip_id: None, create)

    assert registry.register(42, "Ctrl+Taken") == "conflict"

    assert registry.count == 0
    assert actions[42].remove_calls == [True]


def test_clip_global_hotkey_removal_erases_the_kglobalaccel_action():
    actions = {}

    def create(clip_id, sequence):
        action = FakeHotkey(sequence, f"clip-{clip_id}", f"Paste clip {clip_id}")
        actions[clip_id] = action
        return action

    registry = ClipHotkeyRegistry(lambda _clip_id: None, create)
    registry.register(42, "Meta+G")

    registry.remove(42)

    assert registry.count == 0
    assert actions[42].remove_calls == [True]


def test_clip_global_hotkey_prune_erases_actions_for_trimmed_clips():
    actions = {}

    def create(clip_id, sequence):
        action = FakeHotkey(sequence, f"clip-{clip_id}", f"Paste clip {clip_id}")
        actions[clip_id] = action
        return action

    registry = ClipHotkeyRegistry(lambda _clip_id: None, create)
    registry.register(42, "Meta+G")
    registry.register(43, "Meta+H")

    registry.prune({43})

    assert registry.count == 1
    assert actions[42].remove_calls == [True]


def test_clip_global_hotkey_registry_caps_registered_actions():
    registry = ClipHotkeyRegistry(
        lambda _clip_id: None,
        lambda clip_id, sequence: FakeHotkey(sequence, f"clip-{clip_id}", "Paste clip"),
    )
    for clip_id in range(MAX_GLOBAL_CLIP_HOTKEYS):
        assert registry.register(clip_id, f"Meta+{clip_id}") is None

    assert registry.register(999, "Meta+Overflow") == "limit"
