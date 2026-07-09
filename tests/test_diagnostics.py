import subprocess

from keeps.diagnostics import (
    check_kglobalaccel,
    check_klipper,
    check_paste_injector,
    check_session_type,
    check_tesseract,
    check_uinput_access,
    check_wl_paste,
    run_all,
)


def _which(available: set[str]):
    return lambda tool: f"/usr/bin/{tool}" if tool in available else None


def test_check_wl_paste_found():
    check = check_wl_paste(_which({"wl-paste"}))
    assert check.ok is True


def test_check_wl_paste_missing():
    check = check_wl_paste(_which(set()))
    assert check.ok is False


def test_check_paste_injector_wayland_uses_ydotool():
    check = check_paste_injector({"XDG_SESSION_TYPE": "wayland"}, _which({"ydotool"}))
    assert check.name == "ydotool"
    assert check.ok is True


def test_check_paste_injector_x11_uses_xdotool():
    check = check_paste_injector({"XDG_SESSION_TYPE": "x11"}, _which({"xdotool"}))
    assert check.name == "xdotool"
    assert check.ok is True


def test_check_uinput_access_true():
    check = check_uinput_access(lambda path: True)
    assert check.ok is True


def test_check_uinput_access_false():
    check = check_uinput_access(lambda path: False)
    assert check.ok is False


def test_check_session_type_known():
    assert check_session_type({"XDG_SESSION_TYPE": "wayland"}).ok is True
    assert check_session_type({"XDG_SESSION_TYPE": "x11"}).ok is True


def test_check_session_type_unknown():
    assert check_session_type({}).ok is False


def test_check_kglobalaccel_ok(monkeypatch):
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0)

    assert check_kglobalaccel(runner).ok is True


def test_check_kglobalaccel_failure():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1)

    assert check_kglobalaccel(runner).ok is False


def test_check_kglobalaccel_missing_busctl():
    def runner(*args, **kwargs):
        raise OSError("no busctl")

    assert check_kglobalaccel(runner).ok is False


def test_check_klipper_not_running_is_ok():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1)

    check = check_klipper(runner)
    assert check.ok is True


def test_check_klipper_running_is_not_ok():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=0)

    check = check_klipper(runner)
    assert check.ok is False


def test_check_tesseract_optional():
    assert check_tesseract(_which({"tesseract"})).ok is True
    assert check_tesseract(_which(set())).ok is False


def test_run_all_returns_seven_checks():
    def runner(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode=1)

    checks = run_all(
        which=_which({"wl-paste", "ydotool"}),
        runner=runner,
        path_exists=lambda path: True,
        env={"XDG_SESSION_TYPE": "wayland"},
    )
    assert len(checks) == 7
