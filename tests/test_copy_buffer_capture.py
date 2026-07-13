from types import SimpleNamespace

from keeps.capture.wayland import WaylandWatcher
from keeps.copy_buffers import CopyBufferController


def test_wayland_buffer_capture_is_consumed_before_history_store(monkeypatch):
    monkeypatch.setattr(
        "keeps.capture.wayland.QGuiApplication",
        SimpleNamespace(clipboard=lambda: SimpleNamespace(ownsClipboard=lambda: False)),
    )
    captured = []
    stored = []
    watcher = SimpleNamespace(
        _process=SimpleNamespace(readAllStandardOutput=lambda: None),
        _capture_bundle=lambda: ("text", {"text/plain": b"buffer"}),
        _buffer_capture=lambda kind, data: captured.append((kind, data)),
        _store_bundle=lambda kind, data: stored.append((kind, data)),
        guard=SimpleNamespace(consume_skip=lambda: False),
    )

    WaylandWatcher._on_triggered(watcher)

    assert captured == [("text", {"text/plain": b"buffer"})]
    assert stored == []
    assert watcher._buffer_capture is None


def test_copy_restore_forces_the_previous_clipboard_owner(monkeypatch):
    restored = []
    clipboard = SimpleNamespace(
        ownsClipboard=lambda: False,
        setMimeData=lambda mime_data: restored.append(mime_data),
    )
    monkeypatch.setattr(
        "keeps.copy_buffers.QGuiApplication", SimpleNamespace(clipboard=lambda: clipboard)
    )

    CopyBufferController._restore_snapshot({"text/plain": b"before"}, force=True)

    assert len(restored) == 1
    assert bytes(restored[0].data("text/plain")) == b"before"


def test_delayed_paste_restore_does_not_overwrite_a_new_owner(monkeypatch):
    restored = []
    clipboard = SimpleNamespace(
        ownsClipboard=lambda: False,
        setMimeData=lambda mime_data: restored.append(mime_data),
    )
    monkeypatch.setattr(
        "keeps.copy_buffers.QGuiApplication", SimpleNamespace(clipboard=lambda: clipboard)
    )

    CopyBufferController._restore_snapshot({"text/plain": b"before"})

    assert restored == []
