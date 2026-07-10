from keeps.autostart import autostart_path, is_autostart_enabled, set_autostart_enabled
from keeps.desktop_entry import launch_command


def test_disabled_by_default(tmp_path):
    assert is_autostart_enabled(tmp_path) is False


def test_enable_creates_desktop_file(tmp_path):
    set_autostart_enabled(True, tmp_path)
    path = autostart_path(tmp_path)
    assert path.exists()
    assert f"Exec={launch_command()}" in path.read_text()
    assert is_autostart_enabled(tmp_path) is True


def test_disable_removes_desktop_file(tmp_path):
    set_autostart_enabled(True, tmp_path)
    set_autostart_enabled(False, tmp_path)
    assert is_autostart_enabled(tmp_path) is False


def test_disable_when_never_enabled_is_a_noop(tmp_path):
    set_autostart_enabled(False, tmp_path)
    assert is_autostart_enabled(tmp_path) is False
