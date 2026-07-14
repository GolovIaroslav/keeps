import os
import subprocess
import sys

import pytest

from keeps import config


def test_open_settings_recovers_values_from_duplicate_general_sections(tmp_path, monkeypatch):
    config_home = tmp_path / "config"
    settings_path = config_home / "keeps" / "keeps.ini"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        "[%General]\n"
        "language=en\n"
        "max_items=5000\n"
        "\n"
        "[ai]\n"
        "rag_text_enabled=true\n"
        "\n"
        "[%General]\n"
        "max_items=5000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    settings = config.open_settings()

    assert config.get(settings, "general/max_items") == 5000
    assert config.get(settings, "general/language") == "en"
    assert config.get(settings, "ai/rag_text_enabled") is True

    settings.sync()
    assert settings_path.read_text(encoding="utf-8").count("[%General]") == 1


def test_settings_are_read_back_from_the_user_file_after_an_app_update(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    settings = config.open_settings()
    settings.setValue("general/max_items", 5000)
    settings.sync()

    reader = """
from keeps import config

settings = config.open_settings()
print(config.get(settings, "general/max_items"))
"""
    environment = os.environ | {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    reloaded = subprocess.check_output(
        [sys.executable, "-c", reader],
        cwd=str(tmp_path),
        env=environment,
        text=True,
    )

    assert reloaded.strip() == "5000"


def test_concurrent_setting_writes_keep_one_general_section(tmp_path, monkeypatch):
    worker = """
import sys
from keeps import config

key, value = sys.argv[1:]
for _ in range(12):
    config.open_settings().setValue(key, value)
"""
    config_home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    environment = os.environ | {"XDG_CONFIG_HOME": str(config_home)}
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", worker, "general/max_items", "5000"],
            cwd=str(tmp_path),
            env=environment,
        ),
        subprocess.Popen(
            [sys.executable, "-c", worker, "general/language", "en"],
            cwd=str(tmp_path),
            env=environment,
        ),
    ]

    assert [process.wait(timeout=10) for process in workers] == [0, 0]

    settings_path = config.settings_path()
    settings = config.open_settings()
    assert config.get(settings, "general/max_items") == 5000
    assert config.get(settings, "general/language") == "en"
    assert settings_path.read_text(encoding="utf-8").count("[%General]") == 1

PARSE_CASES = [
    ("", []),
    ("eslav", ["eslav"]),
    ("eslav,latin,ch", ["eslav", "latin", "ch"]),
    (" eslav , latin ", ["eslav", "latin"]),
    ("eslav,eslav,latin", ["eslav", "latin"]),
    (",eslav,latin,", ["eslav", "latin"]),
    ("eslav,,latin", ["eslav", "latin"]),
    (",,", []),
]


@pytest.mark.parametrize("value,expected", PARSE_CASES)
def test_parse_ocr_languages(value, expected):
    assert config.parse_ocr_languages(value) == expected


FORMAT_CASES = [
    ([], ""),
    (["eslav"], "eslav"),
    (["eslav", "latin", "ch"], "eslav,latin,ch"),
]


@pytest.mark.parametrize("codes,expected", FORMAT_CASES)
def test_format_ocr_languages(codes, expected):
    assert config.format_ocr_languages(codes) == expected


ROUND_TRIP_CASES = [
    [],
    ["eslav"],
    ["eslav", "latin", "ch"],
    ["a", "b", "c", "d", "eslav"],
]


@pytest.mark.parametrize("codes", ROUND_TRIP_CASES)
def test_round_trip(codes):
    assert config.parse_ocr_languages(config.format_ocr_languages(codes)) == codes
