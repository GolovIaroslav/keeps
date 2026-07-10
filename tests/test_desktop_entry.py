import pytest

from keeps.desktop_entry import applications_path, ensure_installed, launch_command


def test_ensure_installed_creates_desktop_file(tmp_path):
    ensure_installed(tmp_path)
    path = applications_path(tmp_path)
    assert path.exists()
    text = path.read_text()
    assert "Type=Application" in text
    assert f"Exec={launch_command()}" in text


def test_ensure_installed_is_idempotent(tmp_path):
    ensure_installed(tmp_path)
    first_mtime = applications_path(tmp_path).stat().st_mtime_ns
    ensure_installed(tmp_path)
    assert applications_path(tmp_path).stat().st_mtime_ns == first_mtime


@pytest.mark.parametrize(
    ("environ", "on_path", "argv0_is_real_script", "expected"),
    [
        # AppImage wins over everything: $APPIMAGE is the launchable artifact.
        (
            {"APPIMAGE": "/home/u/Applications/keeps.AppImage"},
            True,
            True,
            "/home/u/Applications/keeps.AppImage",
        ),
        # On PATH (distro package): a bare name keeps the entry relocatable.
        ({}, True, False, "keeps"),
        # Source checkout: fall back to the venv console script's absolute path.
        ({}, False, True, "<argv0>"),
        # Nothing works: keep the old bare name rather than something invalid.
        ({}, False, False, "keeps"),
    ],
)
def test_launch_command(tmp_path, environ, on_path, argv0_is_real_script, expected):
    which = (lambda name: f"/usr/bin/{name}") if on_path else (lambda name: None)
    if argv0_is_real_script:
        argv0 = tmp_path / "venv" / "bin" / "keeps"
        argv0.parent.mkdir(parents=True)
        argv0.write_text("#!/usr/bin/env python3\n")
    else:
        argv0 = tmp_path / "nonexistent" / "keeps"
    if expected == "<argv0>":
        expected = str(argv0.resolve())
    assert launch_command(environ, which, str(argv0)) == expected
