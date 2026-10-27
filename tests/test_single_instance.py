"""Prevent duplicate clipboard watchers during concurrent desktop launches."""

import fcntl
import os

from keeps import app


def test_single_instance_acquires_lock_for_daemon(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(app, "_send_message", lambda *_: False)
    monkeypatch.setattr(
        app,
        "_run_daemon",
        lambda show_immediately: called.append(show_immediately) or 0,
    )
    assert app._start_single_instance(str(tmp_path / "keeps.sock"), b"show", show=True) == 0
    assert called == [True]


def test_single_instance_refuses_second_daemon(monkeypatch, tmp_path):
    sock = str(tmp_path / "keeps.sock")
    fd = os.open(sock + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        monkeypatch.setattr(app, "_send_message", lambda *_: False)
        monkeypatch.setattr(app.time, "sleep", lambda _: None)
        monkeypatch.setattr(
            app, "_run_daemon",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("duplicate daemon")),
        )
        assert app._start_single_instance(sock, b"toggle", show=False) == 1
    finally:
        os.close(fd)


def test_single_instance_retries_ipc_while_daemon_initializes(monkeypatch, tmp_path):
    sock = str(tmp_path / "keeps.sock")
    fd = os.open(sock + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        calls = []
        monkeypatch.setattr(
            app, "_send_message",
            lambda _sock, _msg: calls.append(True) or len(calls) == 3,
        )
        monkeypatch.setattr(app.time, "sleep", lambda _: None)
        monkeypatch.setattr(
            app, "_run_daemon",
            lambda **kwargs: (_ for _ in ()).throw(AssertionError("duplicate daemon")),
        )
        assert app._start_single_instance(sock, b"toggle", show=False) == 0
        assert len(calls) == 3
    finally:
        os.close(fd)
