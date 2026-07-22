import pytest

from keeps.desktop_entry import applications_path, ensure_installed, launch_command


def test_ensure_installed_creates_desktop_file(tmp_path):
    ensure_installed(tmp_path)
    path = applications_path(tmp_path)
    assert path.exists()
    text = path.read_text()
    assert "Type=Application" in text
    assert f"Exec={launch_command()}" in text
    assert "StartupWMClass=keeps" in text


def test_ensure_installed_is_idempotent(tmp_path):
    ensure_installed(tmp_path)
    first_mtime = applications_path(tmp_path).stat().st_mtime_ns
    ensure_installed(tmp_path)
    assert applications_path(tmp_path).stat().st_mtime_ns == first_mtime


def test_ensure_installed_preserves_a_live_appimage_launcher(tmp_path):
    image = tmp_path / "keeps.AppImage"
    image.touch()
    path = applications_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(f"[Desktop Entry]\nExec=env APPIMAGELAUNCHER_DISABLE=1 {image}\n")

    ensure_installed(tmp_path)

    assert f"Exec=env APPIMAGELAUNCHER_DISABLE=1 {image}" in path.read_text()


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
        # `uv run keeps`: which("keeps") ALSO succeeds here, because uv
        # prepends the venv's bin dir to PATH for this one child process --
        # but that PATH extension doesn't exist for a future launch from a
        # plain shell or the Applications menu, so the absolute path must
        # still win over the bare name (regression test for the real bug
        # this caused: every dev-daemon run silently clobbered the
        # AppImage-pointing Exec= line with a non-portable "keeps").
        ({}, True, True, "<argv0>"),
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
