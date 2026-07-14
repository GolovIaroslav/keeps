from keeps import config
from keeps.i18n import SUPPORTED_LANGUAGES, translate_text


def test_english_is_the_default_and_eight_languages_are_available():
    assert config.DEFAULTS["general/language"] == "en"
    assert [code for code, _label in SUPPORTED_LANGUAGES] == [
        "en",
        "ru",
        "es",
        "de",
        "fr",
        "pt_BR",
        "zh_CN",
        "ja",
    ]


def test_translations_cover_core_labels_and_fall_back_to_english():
    assert translate_text("ru", "History") == "История"
    assert translate_text("ru", "auto") == "авто"
    assert translate_text("ru", "Dark") == "Тёмная"
    assert translate_text("es", "History") == "Historial"
    assert translate_text("de", "Settings...") == "Einstellungen..."
    assert all(
        translate_text(code, "History") != "History" for code, _label in SUPPORTED_LANGUAGES[1:]
    )
    assert translate_text("ru", "A future label") == "A future label"
