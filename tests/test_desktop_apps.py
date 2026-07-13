from pathlib import Path

from keeps.desktop_apps import command_for_files, installed_applications


def _write_desktop(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Desktop Entry]\nType=Application\n" + body)


def test_installed_applications_use_user_entry_and_skip_hidden_entries(tmp_path):
    user = tmp_path / "user"
    system = tmp_path / "system"
    _write_desktop(
        system / "applications" / "org.example.Editor.desktop",
        "Name=System Editor\nExec=system-editor %F\n",
    )
    _write_desktop(
        user / "applications" / "org.example.Editor.desktop",
        "Name=User Editor\nExec=user-editor %F\n",
    )
    _write_desktop(
        system / "applications" / "hidden.desktop",
        "Name=Hidden\nNoDisplay=true\nExec=hidden\n",
    )

    apps = installed_applications(data_home=user, data_dirs=(system,))

    assert [(app.name, app.exec_line) for app in apps] == [
        ("User Editor", "user-editor %F"),
    ]


def test_command_for_files_expands_desktop_exec_placeholders():
    assert command_for_files("kate --new %F", [Path("/tmp/a file.txt"), "/tmp/b.txt"]) == [
        "kate",
        "--new",
        "/tmp/a file.txt",
        "/tmp/b.txt",
    ]


def test_command_for_files_appends_path_to_a_plain_command():
    assert command_for_files("/usr/bin/kate", ["/tmp/clip.txt"]) == [
        "/usr/bin/kate",
        "/tmp/clip.txt",
    ]
