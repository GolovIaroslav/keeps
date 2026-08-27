"""Compatibility decoding for clipboard text serialized by some producers."""

from __future__ import annotations

import ctypes
import re
import unicodedata
from functools import cache

_UNICODE_4HEX_RE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")
_UNICODE_8HEX_RE = re.compile(r"(?<!\\)\\U([0-9a-fA-F]{8})")

_ISO_WITH_WINDOWS_C1 = {
    "ISO-8859-1": "cp1252",
    "ISO-8859-2": "cp1250",
    "ISO-8859-7": "cp1253",
    "ISO-8859-9": "cp1254",
}
_WINDOWS_LATIN_ENCODINGS = {
    "WINDOWS-1250": "cp1250",
    "WINDOWS-1252": "cp1252",
    "WINDOWS-1254": "cp1254",
}
_TURKISH_DISTINCTIVE = frozenset("ĞğİıŞş")


class _XTextProperty(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.POINTER(ctypes.c_ubyte)),
        ("encoding", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("nitems", ctypes.c_ulong),
    ]


def decode_unicode_escapes(text: str) -> str:
    """Decode literal JSON-style ``\\uXXXX`` and ``\\UXXXXXXXX`` escapes."""
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
    return "".join(result)


@cache
def _load_uchardet():
    """Load the tiny native detector when available (bundled in the AppImage)."""
    try:
        library = ctypes.CDLL("libuchardet.so.0")
    except OSError:
        return None
    library.uchardet_new.restype = ctypes.c_void_p
    library.uchardet_delete.argtypes = [ctypes.c_void_p]
    library.uchardet_handle_data.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]
    library.uchardet_handle_data.restype = ctypes.c_int
    library.uchardet_data_end.argtypes = [ctypes.c_void_p]
    library.uchardet_get_charset.argtypes = [ctypes.c_void_p]
    library.uchardet_get_charset.restype = ctypes.c_char_p
    return library


@cache
def _load_x11():
    try:
        library = ctypes.CDLL("libX11.so.6")
    except OSError:
        return None
    library.XOpenDisplay.argtypes = [ctypes.c_char_p]
    library.XOpenDisplay.restype = ctypes.c_void_p
    library.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    library.XInternAtom.restype = ctypes.c_ulong
    library.Xutf8TextPropertyToTextList.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_XTextProperty),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)),
        ctypes.POINTER(ctypes.c_int),
    ]
    library.Xutf8TextPropertyToTextList.restype = ctypes.c_int
    library.XFreeStringList.argtypes = [ctypes.POINTER(ctypes.c_char_p)]
    library.XCloseDisplay.argtypes = [ctypes.c_void_p]
    return library


def decode_x11_compound_text(data: bytes) -> str | None:
    """Decode X11 COMPOUND_TEXT with Xlib's standards-compliant converter."""
    library = _load_x11()
    if library is None:
        return None
    display = library.XOpenDisplay(None)
    if not display:
        return None
    strings = ctypes.POINTER(ctypes.c_char_p)()
    try:
        atom = library.XInternAtom(display, b"COMPOUND_TEXT", 0)
        if not atom:
            return None
        buffer = ctypes.create_string_buffer(data)
        prop = _XTextProperty(
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)), atom, 8, len(data)
        )
        count = ctypes.c_int()
        status = library.Xutf8TextPropertyToTextList(
            display, ctypes.byref(prop), ctypes.byref(strings), ctypes.byref(count)
        )
        if status != 0 or not strings:
            return None
        return "\n".join(strings[index].decode("utf-8") for index in range(count.value))
    except UnicodeDecodeError:
        return None
    finally:
        if strings:
            library.XFreeStringList(strings)
        library.XCloseDisplay(display)


def _detect_legacy_encoding(data: bytes) -> str | None:
    library = _load_uchardet()
    if library is None:
        return None
    detector = library.uchardet_new()
    if not detector:
        return None
    try:
        if library.uchardet_handle_data(detector, data, len(data)) != 0:
            return None
        library.uchardet_data_end(detector)
        detected = library.uchardet_get_charset(detector)
        return detected.decode("ascii") if detected else None
    finally:
        library.uchardet_delete(detector)


def _decode_detected_legacy(data: bytes, encoding: str) -> str | None:
    try:
        text = data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return None

    # uchardet can report the ISO sibling for short Windows-codepage text.
    # C1 control characters are not printable prose, while the same 0x80-0x9f
    # bytes carry punctuation/letters in the Windows variants. Prefer that
    # deterministic interpretation only when the ISO decode actually exposes
    # C1 controls; otherwise the ISO and Windows decodes are equivalent enough
    # to keep the detector's answer.
    windows_encoding = _ISO_WITH_WINDOWS_C1.get(encoding.upper())
    if windows_encoding and any("\x80" <= char <= "\x9f" for char in text):
        try:
            windows_text = data.decode(windows_encoding)
        except UnicodeDecodeError:
            pass
        else:
            if not any("\x80" <= char <= "\x9f" for char in windows_text):
                return windows_text
    return text


def _decode_windows_latin(data: bytes, detected: str | None) -> str | None:
    """Resolve the narrow CP1250/1252/1254 ambiguity without global guessing."""
    candidates: dict[str, str] = {}
    for encoding in ("cp1250", "cp1252", "cp1254"):
        try:
            candidates[encoding] = data.decode(encoding)
        except UnicodeDecodeError:
            continue
    if not candidates:
        return None

    unique_texts = set(candidates.values())
    if len(unique_texts) == 1:
        return next(iter(unique_texts))
    if len(candidates) == 1:
        return next(iter(candidates.values()))

    detector_choice = _WINDOWS_LATIN_ENCODINGS.get((detected or "").upper())
    # CP1250 has several byte assignments that can look strongly Turkish when
    # decoded as CP1254, so a positive Central-European detector result must
    # win before the Turkish ambiguity heuristic. A real Turkish sample is
    # commonly misreported by uchardet as WINDOWS-1252, though, so 1252 stays
    # eligible for the narrow Turkish override below.
    if detector_choice in {"cp1250", "cp1254"} and detector_choice in candidates:
        return candidates[detector_choice]

    # Turkish-specific bytes can break the CP1252/CP1254 tie when the detector
    # is absent or says CP1252. Require more than one distinct Turkish
    # character because a single byte can legitimately be Icelandic/Western.
    turkish = candidates.get("cp1254")
    if turkish is not None and len(set(turkish) & _TURKISH_DISTINCTIVE) >= 2:
        return turkish

    if detector_choice in candidates:
        return candidates[detector_choice]

    # uchardet occasionally calls short Western text an IBM DOS codepage. If
    # the Windows decoders agree, use their shared Unicode result; otherwise
    # leave the bytes unresolved rather than inventing a language preference.
    if detected and detected.upper().startswith("IBM"):
        western = candidates.get("cp1252")
        turkish = candidates.get("cp1254")
        if western is not None and western == turkish:
            return western
    return None


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

    # 2. X11 COMPOUND_TEXT is stateful; let Xlib implement the actual standard
    # rather than treating only its first escape sequence as one fixed codec.
    if encoding_hint == "x11-compound-text":
        compound_text = decode_x11_compound_text(data)
        if compound_text is not None:
            return compound_text

    # 3. An explicit MIME charset is more reliable than statistical guessing.
    if encoding_hint:
        try:
            return data.decode(encoding_hint)
        except (LookupError, UnicodeDecodeError):
            pass

    # 4. UTF-8 (strict)
    try:
        res = data.decode("utf-8")
        if res.startswith("\ufeff"):
            res = res[1:]
        return res
    except UnicodeDecodeError:
        pass

    # 5. UTF-16 without BOM heuristic
    if len(data) >= 4 and len(data) % 2 == 0:
        if data[1::2].count(0) > len(data) // 4:
            try:
                candidate = data.decode("utf-16-le")
                if candidate.isprintable() or any(char in candidate for char in "\r\n\t"):
                    return candidate
            except UnicodeDecodeError:
                pass
        if data[0::2].count(0) > len(data) // 4:
            try:
                candidate = data.decode("utf-16-be")
                if candidate.isprintable() or any(char in candidate for char in "\r\n\t"):
                    return candidate
            except UnicodeDecodeError:
                pass

    # 6. Legacy encodings are genuinely ambiguous without metadata; use a
    # mature detector rather than a home-grown script/frequency guess. The
    # AppImage bundles libuchardet; source installs without it still retain
    # exact UTF/BOM/declared-charset handling above and fail visibly below.
    detected = _detect_legacy_encoding(data)
    if detected:
        if detected.upper() in _WINDOWS_LATIN_ENCODINGS or detected.upper().startswith("IBM"):
            latin = _decode_windows_latin(data, detected)
            if latin is not None:
                return latin
        decoded = _decode_detected_legacy(data, detected)
        if decoded is not None:
            return decoded
    else:
        latin = _decode_windows_latin(data, None)
        if latin is not None:
            return latin

    return data.decode("utf-8", errors="replace")


def normalize_plain_text(data: bytes, encoding_hint: str | None = None) -> bytes:
    """Return canonical UTF-8 NFC plain text, decoding legacy encodings and escapes."""
    text = decode_bytes_smart(data, encoding_hint)
    text = decode_unicode_escapes(text)
    text = unicodedata.normalize("NFC", text)
    return text.encode("utf-8")
