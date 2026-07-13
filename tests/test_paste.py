import subprocess

import pytest

from keeps.paste import (
    active_app_class,
    inject_paste,
    injection_environment,
    notify_paste_unavailable,
    paste_command,
    session_backend,
    shortcut_for_app,
)

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


@pytest.mark.parametrize(
    "backend,expected",
    [
        (
            "wayland",
            ["ydotool", "key", "29:1", "42:1", "47:1", "47:0", "42:0", "29:0"],
        ),
        ("x11", ["xdotool", "key", "ctrl+shift+v"]),
    ],
)
def test_paste_command_supports_terminal_chord(backend, expected):
    assert paste_command(backend, lambda tool: f"/usr/bin/{tool}", "ctrl+shift+v") == expected


def test_active_app_class_uses_backend_window_tool_and_normalizes():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="  Org.KDE.Konsole\n")

    assert active_app_class("wayland", lambda tool: f"/usr/bin/{tool}", runner) == "org.kde.konsole"
    assert calls[0][0] == ["kdotool", "getactivewindow", "getwindowclassname"]
    assert calls[0][1]["timeout"] <= 0.25


def test_active_app_class_falls_back_on_missing_tool_timeout_or_empty_output():
    assert active_app_class("wayland", lambda _tool: None, subprocess.run) is None

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    assert active_app_class("x11", lambda tool: f"/usr/bin/{tool}", timeout) is None
    assert active_app_class(
        "x11",
        lambda tool: f"/usr/bin/{tool}",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, stdout=" \n"),
    ) is None


@pytest.mark.parametrize("stdout", ["one\ntwo\n", "class with spaces", "!", "x" * 256])
def test_active_app_class_rejects_malformed_output(stdout):
    def result(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout)

    assert active_app_class("wayland", lambda tool: f"/usr/bin/{tool}", result) is None


def test_active_app_class_falls_back_on_decode_failure():
    def broken_decode(command, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")

    assert active_app_class("wayland", lambda tool: f"/usr/bin/{tool}", broken_decode) is None


def test_shortcut_for_app_uses_casefolded_editable_json_mapping():
    mapping = '{"org.kde.konsole":"ctrl+shift+v","firefox":"ctrl+v"}'
    assert shortcut_for_app("ORG.KDE.KONSOLE", mapping) == "ctrl+shift+v"
    assert shortcut_for_app("firefox", mapping) == "ctrl+v"
    assert shortcut_for_app("unknown", mapping) == "ctrl+v"
    assert shortcut_for_app("konsole", "not json") == "ctrl+v"


def test_injection_environment_repairs_stale_ydotool_socket():
    runtime_socket = "/run/user/1000/.ydotool_socket"
    result = injection_environment(
        "wayland",
        {
            "YDOTOOL_SOCKET": "/tmp/.ydotool_socket",
            "XDG_RUNTIME_DIR": "/run/user/1000",
        },
        path_exists=lambda path: str(path) == runtime_socket,
    )
    assert result["YDOTOOL_SOCKET"] == runtime_socket


def test_injection_environment_preserves_valid_explicit_socket():
    result = injection_environment(
        "wayland",
        {"YDOTOOL_SOCKET": "/custom/socket", "XDG_RUNTIME_DIR": "/run/user/1000"},
        path_exists=lambda path: str(path)
        in {"/custom/socket", "/run/user/1000/.ydotool_socket"},
    )
    assert result["YDOTOOL_SOCKET"] == "/custom/socket"


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


def test_inject_paste_passes_a_timeout_to_the_runner():
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs)

    inject_paste("x11", which=lambda tool: "/usr/bin/xdotool", runner=runner)
    assert calls[0]["timeout"] > 0


def test_inject_paste_hung_process_returns_false_instead_of_blocking_forever():
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

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
