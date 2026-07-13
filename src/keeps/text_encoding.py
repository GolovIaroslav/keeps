"""Compatibility decoding for clipboard text serialized by some producers."""

from __future__ import annotations

import re

_UNICODE_ESCAPE_RE = re.compile(r"(?<!\\)\\u([0-9a-fA-F]{4})")


def decode_unicode_escapes(text: str) -> str:
    """Decode literal JSON-style ``\\uXXXX`` escapes without touching other text."""
    result: list[str] = []
    position = 0
    while match := _UNICODE_ESCAPE_RE.search(text, position):
        result.append(text[position : match.start()])
        codepoint = int(match.group(1), 16)
        end = match.end()

        if 0xD800 <= codepoint <= 0xDBFF:
            low_match = _UNICODE_ESCAPE_RE.match(text, end)
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


def normalize_plain_text(data: bytes) -> bytes:
    """Return UTF-8 text, decoding legacy literal Unicode escapes on read."""
    return decode_unicode_escapes(data.decode("utf-8", errors="replace")).encode("utf-8")
