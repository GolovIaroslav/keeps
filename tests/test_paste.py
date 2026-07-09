import pytest

from keeps.paste import inject_paste, notify_paste_unavailable, paste_command, session_backend

BACKEND_CASES = [
    ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
    ({"XDG_SESSION_TYPE": "x11"}, "x11"),
    ({"XDG_SESSION_TYPE": ""}, "x11"),
    ({}, "x11"),
]


@pytest.mark.parametrize("env,expected", BACKEND_CASES)
def test_session_backend(env, expected):
    assert session_backend(env) == expected


def test_paste_command_wayland_uses_ydotool_raw_keycodes():
    command = paste_command("wayland", which=lambda tool: "/usr/bin/ydotool")
    assert command == ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"]


def test_paste_command_x11_uses_xdotool_named_key():
    command = paste_command("x11", which=lambda tool: "/usr/bin/xdotool")
    assert command == ["xdotool", "key", "ctrl+v"]


def test_paste_command_missing_tool_returns_none():
    assert paste_command("wayland", which=lambda tool: None) is None
    assert paste_command("x11", which=lambda tool: None) is None


def test_inject_paste_missing_tool_returns_false_without_running():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)

    result = inject_paste("wayland", which=lambda tool: None, runner=runner)
    assert result is False
    assert calls == []


def test_inject_paste_runs_command_and_returns_true():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)

    result = inject_paste("x11", which=lambda tool: "/usr/bin/xdotool", runner=runner)
    assert result is True
    assert calls == [["xdotool", "key", "ctrl+v"]]


def test_inject_paste_process_failure_returns_false():
    def runner(command, **kwargs):
        raise OSError("boom")

    result = inject_paste("x11", which=lambda tool: "/usr/bin/xdotool", runner=runner)
    assert result is False


def test_notify_paste_unavailable_noop_without_notify_send(monkeypatch):
    calls = []
    monkeypatch.setattr("keeps.paste.subprocess.run", lambda *a, **k: calls.append(a))
    notify_paste_unavailable("wayland", which=lambda tool: None)
    assert calls == []


def test_notify_paste_unavailable_calls_notify_send(monkeypatch):
    calls = []
    monkeypatch.setattr("keeps.paste.subprocess.run", lambda *a, **k: calls.append(a))
    notify_paste_unavailable("wayland", which=lambda tool: "/usr/bin/notify-send")
    assert len(calls) == 1
    assert calls[0][0][0] == "notify-send"
