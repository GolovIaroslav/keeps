from keeps.desktop_entry import applications_path, ensure_installed


def test_ensure_installed_creates_desktop_file(tmp_path):
    ensure_installed(tmp_path)
    path = applications_path(tmp_path)
    assert path.exists()
    assert "Type=Application" in path.read_text()
    assert "Exec=keeps" in path.read_text()


def test_ensure_installed_is_idempotent(tmp_path):
    ensure_installed(tmp_path)
    first_mtime = applications_path(tmp_path).stat().st_mtime_ns
    ensure_installed(tmp_path)
    assert applications_path(tmp_path).stat().st_mtime_ns == first_mtime
