"""Tests for text_encoding: Slovak characters, multi-language support, escapes, and fallbacks."""

import os
import unicodedata

import pytest

from keeps.text_encoding import (
    decode_bytes_smart,
    decode_unicode_escapes,
    decode_x11_compound_text,
    normalize_plain_text,
)

SLOVAK_LOWER = "áäčďéíĺľňóôŕšťúýž"
SLOVAK_UPPER = "ÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ"
SLOVAK_PHRASE = "Analýza a zložitosť algoritmov. Princípy informačných systémov."


def test_slovak_utf8_roundtrip():
    data = f"{SLOVAK_LOWER} {SLOVAK_UPPER}".encode()
    assert normalize_plain_text(data).decode("utf-8") == f"{SLOVAK_LOWER} {SLOVAK_UPPER}"


def test_slovak_nfd_normalized_to_nfc():
    # NFD: decomposed characters (base + combining diacritics)
    nfd_text = unicodedata.normalize("NFD", SLOVAK_PHRASE)
    assert nfd_text != SLOVAK_PHRASE  # Verify it is actually decomposed
    result = normalize_plain_text(nfd_text.encode("utf-8")).decode("utf-8")
    assert result == SLOVAK_PHRASE
    assert unicodedata.is_normalized("NFC", result)


def test_slovak_cp1250_encoding_fallback():
    # When text arrives encoded as Windows-1250 (Central European)
    cp1250_bytes = SLOVAK_PHRASE.encode("cp1250")
    result = normalize_plain_text(cp1250_bytes).decode("utf-8")
    assert result == SLOVAK_PHRASE


def test_slovak_iso8859_2_encoding_fallback():
    iso_bytes = "Analýza a zložitosť".encode("iso-8859-2")
    result = normalize_plain_text(iso_bytes).decode("utf-8")
    assert result == "Analýza a zložitosť"


def test_unicode_4hex_escapes():
    escaped = r"Anal\u00fdza a zlo\u017eitos\u0165"
    assert decode_unicode_escapes(escaped) == "Analýza a zložitosť"


def test_unicode_8hex_escapes():
    escaped = r"Po\U0000010D\U000000EDta\U0000010D \U0001F680"
    assert decode_unicode_escapes(escaped) == "Počítač 🚀"


def test_unicode_surrogate_pairs():
    escaped = r"Rocket: \uD83D\uDE80, Face: \uD83D\uDE00"
    assert decode_unicode_escapes(escaped) == "Rocket: 🚀, Face: 😀"


def test_plain_text_numeric_html_entities_are_preserved_literally():
    text = 'const x = "&#123;"; Po&#269; &#x010d;'
    assert decode_unicode_escapes(text) == text
    assert normalize_plain_text(text.encode()) == text.encode()


def test_russian_cyrillic_utf8():
    text = "Привет, мир! Как дела? «Вместе»"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text


def test_russian_cp1251_fallback():
    text = "Привет мир"
    cp1251_bytes = text.encode("cp1251")
    assert normalize_plain_text(cp1251_bytes).decode("utf-8") == text


def test_x11_compound_text_cyrillic():
    if not os.environ.get("DISPLAY"):
        pytest.skip("X11 display is required for the native COMPOUND_TEXT converter")
    # ISO-8859-5 with \x1b-L prefix is a valid COMPOUND_TEXT payload.
    compound = b"\x1b-L" + "Привет мир".encode("iso-8859-5")
    assert decode_x11_compound_text(compound) == "Привет мир"
    assert normalize_plain_text(compound, "x11-compound-text").decode() == "Привет мир"


def test_german_special_characters():
    text = "Guten Tag, schöne Grüße aus München! ÄÖÜ äöü ß"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1252")).decode("utf-8") == text


def test_french_special_characters():
    text = "«Déjà vu» à Noël dans l'œuvre de François"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1252")).decode("utf-8") == text


def test_spanish_special_characters():
    text = "¡Hola! ¿Cómo estás, señor? Año, pingüino"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1252")).decode("utf-8") == text


def test_polish_special_characters():
    text = "Zażółć gęślą jaźń. ĄĆĘŁŃÓŚŹŻ"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1250")).decode("utf-8") == text


def test_czech_special_characters():
    text = "Příliš žluťoučký kůň úpěl ďábelské ódy."
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1250")).decode("utf-8") == text


def test_greek_characters():
    text = "Ελληνική Δημοκρατία — Καλημέρα κόσμε"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1253")).decode("utf-8") == text


def test_turkish_characters():
    text = "İstanbul, Türkçe, ç, ğ, ı, ö, ş, ü, Ç, Ğ, İ, Ö, Ş, Ü"
    assert normalize_plain_text(text.encode("utf-8")).decode("utf-8") == text
    assert normalize_plain_text(text.encode("cp1254")).decode("utf-8") == text


def test_cjk_and_arabic_utf8():
    chinese = "你好世界，人工智能剪贴板"
    japanese = "こんにちは世界、クリップボード"
    korean = "안녕하세요 세계, 클립보드"
    arabic = "مرحبا بالعالم"

    for phrase in (chinese, japanese, korean, arabic):
        assert normalize_plain_text(phrase.encode("utf-8")).decode("utf-8") == phrase


@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("cp1252", "Price £12.50 — “quoted”"),
        ("cp1252", "Guten Tag, schöne Grüße aus München! ÄÖÜ äöü ß"),
        ("cp1250", "Příliš žluťoučký kůň úpěl ďábelské ódy."),
        ("cp1250", "Zażółć gęślą jaźń. ĄĆĘŁŃÓŚŹŻ"),
        ("cp1251", "Съешь ещё этих мягких французских булок"),
        ("koi8-r", "Съешь ещё этих мягких французских булок"),
        ("cp1253", "Καλημέρα κόσμε"),
        ("cp1254", "İstanbul, Türkçe, ğ, ı, ş, Ğ, İ, Ş"),
        ("cp1256", "مرحبا بالعالم"),
        ("gb18030", "你好世界，人工智能剪贴板"),
        ("shift_jis", "こんにちは世界、クリップボード"),
        ("euc-kr", "안녕하세요 세계, 클립보드"),
    ],
)
def test_unlabelled_legacy_encoding_detection(encoding, text):
    assert normalize_plain_text(text.encode(encoding)).decode("utf-8") == text


def test_windows_latin_detector_choice_beats_cross_codepage_heuristic(monkeypatch):
    # 0xDE/0xFE are Romanian T-comma/cedilla characters in CP1250 but
    # Turkish S-cedilla in CP1254. If uchardet has already identified CP1250,
    # the ambiguity heuristic must not override that explicit detector result.
    import keeps.text_encoding as text_encoding

    raw = bytes.fromhex("defe")
    monkeypatch.setattr(
        text_encoding, "_detect_legacy_encoding", lambda _data: "WINDOWS-1250"
    )

    assert decode_bytes_smart(raw) == raw.decode("cp1250")


def test_bom_handling():
    text = "Analýza a zložitosť"
    # UTF-8 with BOM
    utf8_bom = b"\xef\xbb\xbf" + text.encode("utf-8")
    assert normalize_plain_text(utf8_bom).decode("utf-8") == text

    # UTF-16-LE with BOM
    utf16_le = b"\xff\xfe" + text.encode("utf-16-le")
    assert normalize_plain_text(utf16_le).decode("utf-8") == text

    # UTF-16-BE with BOM
    utf16_be = b"\xfe\xff" + text.encode("utf-16-be")
    assert normalize_plain_text(utf16_be).decode("utf-8") == text


def test_empty_and_ascii():
    assert normalize_plain_text(b"") == b""
    assert normalize_plain_text(b"Simple ASCII text 123!") == b"Simple ASCII text 123!"
    assert decode_bytes_smart(b"") == ""
