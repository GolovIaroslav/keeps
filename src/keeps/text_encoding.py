"""Compatibility decoding for clipboard text serialized by some producers."""

from __future__ import annotations

import re
import unicodedata

_UNICODE_4HEX_RE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")
_UNICODE_8HEX_RE = re.compile(r"(?<!\\)\\U([0-9a-fA-F]{8})")
_NUMERIC_ENTITY_RE = re.compile(r"&#(?:(\d+)|x([0-9a-fA-F]+));")

_COMPOUND_TEXT_ENCODINGS = {
    b"\x1b-A": "iso-8859-1",
    b"\x1b-B": "iso-8859-2",
    b"\x1b-C": "iso-8859-3",
    b"\x1b-D": "iso-8859-4",
    b"\x1b-L": "iso-8859-5",
    b"\x1b-G": "iso-8859-7",
    b"\x1b-H": "iso-8859-8",
    b"\x1b-M": "iso-8859-9",
}

# Candidate encodings when UTF-8 decoding fails
_FALLBACK_ENCODINGS = (
    "cp1250",  # Central / Eastern European: Slovak, Czech, Polish, Hungarian, etc.
    "cp1252",  # Western European: German, French, Spanish, Italian, Portuguese
    "iso-8859-2",  # Latin-2
    "cp1251",  # Cyrillic: Russian, Ukrainian, Belarusian, Bulgarian, Serbian
    "cp1253",  # Greek
    "iso-8859-15",  # Latin-9
    "iso-8859-1",  # Latin-1
    "cp1254",  # Turkish
    "cp1256",  # Arabic
    "koi8-r",  # Russian Cyrillic
    "iso-8859-5",  # Cyrillic
    "gb18030",  # Chinese
    "shift_jis",  # Japanese
    "euc-kr",  # Korean
)

_CYRILLIC_VOWELS = frozenset("аеёиоуыэюяАЕЁИОУЫЭЮЯ")
_GREEK_VOWELS = frozenset("αεηιουωάέήίόύώΑΕΗΙΟΥΩΆΈΉΊΌΎΏ")


def decode_unicode_escapes(text: str) -> str:
    """Decode literal JSON-style ``\\uXXXX`` and ``\\UXXXXXXXX`` escapes and HTML entities."""
    # 1. Decode \UXXXXXXXX (8 hex digits, e.g. \U0000010D for č or \U0001F600 for emojis)
    def _replace_8hex(match: re.Match[str]) -> str:
        try:
            cp = int(match.group(1), 16)
            if 0 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                return chr(cp)
        except (ValueError, OverflowError):
            pass
        return match.group(0)

    text = _UNICODE_8HEX_RE.sub(_replace_8hex, text)

    # 2. Decode \uXXXX (4 hex digits) with surrogate pair support
    result: list[str] = []
    position = 0
    while match := _UNICODE_4HEX_RE.search(text, position):
        result.append(text[position : match.start()])
        codepoint = int(match.group(1), 16)
        end = match.end()

        if 0xD800 <= codepoint <= 0xDBFF:
            low_match = _UNICODE_4HEX_RE.match(text, end)
            if low_match is not None:
                low = int(low_match.group(1), 16)
                if 0xDC00 <= low <= 0xDFFF:
                    codepoint = 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
                    end = low_match.end()
                else:
                    result.append(match.group(0))
                    position = end
                    continue
            else:
                result.append(match.group(0))
                position = end
                continue
        elif 0xDC00 <= codepoint <= 0xDFFF:
            result.append(match.group(0))
            position = end
            continue

        result.append(chr(codepoint))
        position = end

    result.append(text[position:])
    text = "".join(result)

    # 3. Decode numeric character references (&#269;, &#x010d;)
    def _replace_entity(match: re.Match[str]) -> str:
        try:
            if match.group(1):
                cp = int(match.group(1), 10)
            else:
                cp = int(match.group(2), 16)
            if 0 <= cp <= 0x10FFFF and not (0xD800 <= cp <= 0xDFFF):
                return chr(cp)
        except (ValueError, OverflowError):
            pass
        return match.group(0)

    text = _NUMERIC_ENTITY_RE.sub(_replace_entity, text)
    return text


def _evaluate_candidate_text(text: str, enc: str) -> float:
    if not text:
        return 0.0

    score = 0.0
    scripts = {"LATIN": 0, "CYRILLIC": 0, "GREEK": 0, "ARABIC": 0, "OTHER": 0}
    cyrillic_vowels = 0
    greek_vowels = 0

    for ch in text:
        if ch in "\r\n\t":
            score += 1.0
            continue
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            if name.startswith("LATIN"):
                scripts["LATIN"] += 1
            elif name.startswith("CYRILLIC"):
                scripts["CYRILLIC"] += 1
                if ch in _CYRILLIC_VOWELS:
                    cyrillic_vowels += 1
            elif name.startswith("GREEK"):
                scripts["GREEK"] += 1
                if ch in _GREEK_VOWELS:
                    greek_vowels += 1
            elif name.startswith("ARABIC"):
                scripts["ARABIC"] += 1
            else:
                scripts["OTHER"] += 1
            score += 2.0
        elif unicodedata.category(ch).startswith(("P", "N", "Z")):
            score += 1.0
        elif unicodedata.category(ch).startswith("C"):
            score -= 100.0
        else:
            score += 0.5

    # 1. Heavily penalize mixed scripts
    active_scripts = [k for k, v in scripts.items() if v > 0]
    total_letters = sum(scripts.values())
    if len(active_scripts) > 1 and total_letters >= 3:
        dominant_count = max(scripts.values())
        minority_count = total_letters - dominant_count
        score -= 50.0 * minority_count

    # 2. Casing sanity: penalize uppercase inside or at the end of lowercase words (e.g. fooЮ)
    for word in re.findall(r"\b\w+\b", text):
        if len(word) >= 3:
            if word[:-1].islower() and word[-1].isupper():
                score -= 30.0
            for i in range(1, len(word) - 1):
                if word[i].isupper() and word[i - 1].islower() and word[i + 1].islower():
                    score -= 20.0

    # 3. Cyrillic vs Greek vowel ratio check
    if scripts["CYRILLIC"] >= 3:
        v_ratio = cyrillic_vowels / scripts["CYRILLIC"]
        if 0.25 <= v_ratio <= 0.60:
            score += 20.0
        else:
            score -= 20.0
    if scripts["GREEK"] >= 3:
        v_ratio = greek_vowels / scripts["GREEK"]
        if 0.25 <= v_ratio <= 0.60:
            score += 20.0
        else:
            score -= 20.0

    # 4. Spanish inverted punctuation bonus
    if ("¡" in text and "!" in text) or ("¿" in text and "?" in text):
        if enc == "cp1252":
            score += 25.0

    # 5. Distinctive characters
    if enc in ("cp1250", "iso-8859-2"):
        if any(c in text for c in "čďĺľňšťžřůěřąćęłńśźż"):
            score += 15.0
        if re.search(r"\b[ŕŔ]\b|j[ŕŔ]", text):
            score -= 20.0

    if enc in ("cp1252", "iso-8859-15"):
        if any(c in text for c in "àèêëîïôùûçœæñß"):
            score += 15.0

    if enc == "cp1254" and any(c in text for c in "ğĞşŞİ"):
        score += 20.0

    return score


def decode_bytes_smart(data: bytes, encoding_hint: str | None = None) -> str:
    """Decode raw clipboard bytes using smart encoding detection and fallbacks."""
    if not data:
        return ""

    # 1. BOM checks (UTF-8-SIG, UTF-16)
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass

    # 2. An explicit MIME charset is more reliable than statistical guessing.
    if encoding_hint:
        try:
            return data.decode(encoding_hint)
        except (LookupError, UnicodeDecodeError):
            pass

    # 3. UTF-8 (strict)
    try:
        res = data.decode("utf-8")
        if res.startswith("\ufeff"):
            res = res[1:]
        return res
    except UnicodeDecodeError:
        pass

    # 4. UTF-16 without BOM heuristic
    if len(data) >= 4 and len(data) % 2 == 0:
        if data[1::2].count(0) > len(data) // 4:
            try:
                candidate = data.decode("utf-16-le")
                if _evaluate_candidate_text(candidate, "utf-16-le") > 0:
                    return candidate
            except UnicodeDecodeError:
                pass
        if data[0::2].count(0) > len(data) // 4:
            try:
                candidate = data.decode("utf-16-be")
                if _evaluate_candidate_text(candidate, "utf-16-be") > 0:
                    return candidate
            except UnicodeDecodeError:
                pass

    # 5. X11 Compound text
    for prefix, enc in _COMPOUND_TEXT_ENCODINGS.items():
        if data.startswith(prefix):
            try:
                return data[len(prefix) :].decode(enc)
            except UnicodeDecodeError:
                pass

    # 6. Candidate regional encodings with scoring
    best_text = None
    best_score = -10000.0
    for enc in _FALLBACK_ENCODINGS:
        try:
            text = data.decode(enc)
            # Scoring is Python-level character work; cap it so a large
            # legacy-encoded clipboard item cannot stall capture for seconds.
            score = _evaluate_candidate_text(text[:8192], enc)
            if score > best_score:
                best_score = score
                best_text = text
        except UnicodeDecodeError:
            continue

    if best_text is not None and best_score > 0.0:
        return best_text

    return data.decode("utf-8", errors="replace")


def normalize_plain_text(data: bytes, encoding_hint: str | None = None) -> bytes:
    """Return canonical UTF-8 NFC plain text, decoding legacy encodings and escapes."""
    text = decode_bytes_smart(data, encoding_hint)
    text = decode_unicode_escapes(text)
    text = unicodedata.normalize("NFC", text)
    return text.encode("utf-8")
