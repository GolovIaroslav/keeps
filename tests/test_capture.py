import pytest

from keeps.capture.base import (
    MIME_HTML,
    MIME_IMAGE,
    MIME_PLAIN,
    MIME_URI_LIST,
    SelfSetGuard,
    build_bundle,
    detect_kind,
    html_has_real_formatting,
    should_store,
)
from keeps.store import Store

DETECT_CASES = [
    ({MIME_PLAIN}, "text"),
    ({MIME_PLAIN, MIME_HTML}, "html"),
    ({MIME_URI_LIST, MIME_PLAIN}, "files"),
    ({MIME_IMAGE}, "image"),
    ({MIME_IMAGE, MIME_URI_LIST, MIME_PLAIN}, "image"),  # image wins over files/text
    (set(), None),
    ({"application/x-nonsense"}, None),
]


@pytest.mark.parametrize("available,expected_kind", DETECT_CASES)
def test_detect_kind(available, expected_kind):
    assert detect_kind(available) == expected_kind


HTML_FORMATTING_CASES = [
    (b"<b>bold</b>", True),
    (b"<html><body><pre>plain prose</pre></body></html>", False),
    (b"<ul><li>item</li></ul>", True),
    (b"<span><div><font>plain</font></div></span>", False),
]

# Mirrors a real mis-kinded chat-AI browser response wrapped in <pre> for monospace display.
PRODUCTION_STYLE_HTML = """<html><body><pre>
Это обычный ответ ассистента без жирного шрифта или ссылок.
This is plain prose copied from a browser-based chat interface.
Несколько строк текста, но не настоящее rich formatting.
</pre></body></html>""".encode()


@pytest.mark.parametrize("html_bytes,expected", HTML_FORMATTING_CASES)
def test_html_has_real_formatting(html_bytes, expected):
    assert html_has_real_formatting(html_bytes) is expected


DETECT_HTML_CONTENT_CASES = [
    (
        {MIME_HTML, MIME_PLAIN},
        b"<html><body><p>hello <b>world</b></p></body></html>",
        "html",
    ),
    (
        {MIME_HTML, MIME_PLAIN},
        b"<html><body><pre>plain prose, no formatting</pre></body></html>",
        "text",
    ),
    (
        {MIME_HTML},
        b"<html><body><pre>plain prose, no formatting</pre></body></html>",
        "html",
    ),
    ({MIME_HTML, MIME_PLAIN}, b"<ul><li>item</li></ul>", "html"),
    (
        {MIME_HTML, MIME_PLAIN},
        b"<span><div><font>plain prose</font></div></span>",
        "text",
    ),
    ({MIME_HTML, MIME_PLAIN}, PRODUCTION_STYLE_HTML, "text"),
]


@pytest.mark.parametrize("available,html_bytes,expected_kind", DETECT_HTML_CONTENT_CASES)
def test_detect_kind_uses_html_content(available, html_bytes, expected_kind):
    assert detect_kind(available, html_bytes) == expected_kind


def test_build_bundle_reads_only_relevant_mimes():
    reads = []

    def reader(mime: str) -> bytes:
        reads.append(mime)
        if mime == MIME_HTML:
            return b"<b>payload</b>"
        return b"payload-" + mime.encode()

    result = build_bundle({MIME_PLAIN, MIME_HTML, "text/rtf"}, reader)

    assert result is not None
    kind, mime_data = result
    assert kind == "html"
    assert reads == [MIME_HTML, MIME_PLAIN]
    assert mime_data == {
        MIME_HTML: b"<b>payload</b>",
        MIME_PLAIN: b"payload-" + MIME_PLAIN.encode(),
    }


def test_build_bundle_downgrades_unformatted_html_without_rereading_it():
    reads = []
    mime_data = {
        MIME_HTML: b"<html><body><pre>plain prose</pre></body></html>",
        MIME_PLAIN: b"plain prose",
    }

    def reader(mime: str) -> bytes:
        reads.append(mime)
        return mime_data[mime]

    result = build_bundle({MIME_HTML, MIME_PLAIN}, reader)

    assert result == ("text", {MIME_PLAIN: mime_data[MIME_PLAIN]})
    assert reads == [MIME_HTML, MIME_PLAIN]


@pytest.mark.parametrize(
    ("available", "expected_kind", "expected_reads"),
    [
        ({MIME_IMAGE, MIME_HTML, MIME_PLAIN}, "image", [MIME_IMAGE]),
        (
            {MIME_URI_LIST, MIME_HTML, MIME_PLAIN},
            "files",
            [MIME_URI_LIST, MIME_PLAIN],
        ),
    ],
)
def test_build_bundle_does_not_read_html_when_higher_priority_kind_wins(
    available, expected_kind, expected_reads
):
    reads = []

    def reader(mime: str) -> bytes:
        reads.append(mime)
        return b"payload-" + mime.encode()

    result = build_bundle(available, reader)

    assert result is not None
    assert result[0] == expected_kind
    assert reads == expected_reads


def test_build_bundle_unknown_kind_returns_none():
    assert build_bundle(set(), lambda mime: b"") is None


def test_build_bundle_over_size_cap_returns_none():
    def reader(mime: str) -> bytes:
        return b"x" * (2 * 1024 * 1024)

    result = build_bundle({MIME_PLAIN}, reader, max_item_mb=1)

    assert result is None


def test_build_bundle_under_size_cap_passes():
    def reader(mime: str) -> bytes:
        return b"x" * 1024

    result = build_bundle({MIME_PLAIN}, reader, max_item_mb=1)

    assert result is not None


def test_build_bundle_can_preserve_small_extra_mime_formats():
    extra_mime = "application/x-editor-state"
    mime_bytes = {
        MIME_PLAIN: b"hello",
        extra_mime: b"opaque editor state",
        "application/x-too-large": b"x" * (1024 * 1024 + 1),
    }

    result = build_bundle(
        set(mime_bytes),
        lambda mime: mime_bytes[mime],
        store_all_formats=True,
    )

    assert result == (
        "text",
        {MIME_PLAIN: b"hello", extra_mime: b"opaque editor state"},
    )


def test_extra_mime_formats_respect_the_existing_total_item_cap():
    first_extra = "application/a-small"
    second_extra = "application/z-small"
    mime_bytes = {
        MIME_PLAIN: b"p" * (600 * 1024),
        first_extra: b"a" * (400 * 1024),
        second_extra: b"z" * (400 * 1024),
    }

    result = build_bundle(
        set(mime_bytes),
        lambda mime: mime_bytes[mime],
        max_item_mb=1,
        store_all_formats=True,
    )

    assert result == (
        "text",
        {MIME_PLAIN: mime_bytes[MIME_PLAIN], first_extra: mime_bytes[first_extra]},
    )


def test_self_set_guard_skips_once_within_window():
    guard = SelfSetGuard(window_seconds=1.0)
    guard.mark_self_set()

    assert guard.consume_skip() is True
    assert guard.consume_skip() is False  # only the next event is skipped


def test_self_set_guard_no_skip_without_mark():
    guard = SelfSetGuard()
    assert guard.consume_skip() is False


SHOULD_STORE_CASES = [
    ("text", True, True, True, True),
    ("html", True, True, True, True),
    ("html", False, True, True, False),
    ("image", True, True, True, True),
    ("image", True, False, True, False),
    ("files", True, True, True, True),
    ("files", True, True, False, False),
]


@pytest.mark.parametrize("kind,store_html,store_images,store_files,expected", SHOULD_STORE_CASES)
def test_should_store(kind, store_html, store_images, store_files, expected):
    assert should_store(kind, store_html, store_images, store_files) == expected


BUNDLE_TO_CLIP_CASES = [
    ({MIME_PLAIN}, {MIME_PLAIN: b"hello"}, "text"),
    (
        {MIME_PLAIN, MIME_HTML},
        {MIME_PLAIN: b"hello", MIME_HTML: b"<b>hello</b>"},
        "html",
    ),
    ({MIME_URI_LIST}, {MIME_URI_LIST: b"file:///a.txt"}, "files"),
]


@pytest.mark.parametrize("available,mime_bytes,expected_kind", BUNDLE_TO_CLIP_CASES)
def test_bundle_roundtrips_into_store(tmp_path, available, mime_bytes, expected_kind):
    """End-to-end: build_bundle() output stored via Store.add() produces a matching Clip."""
    store = Store(tmp_path / "keeps.db")
    try:
        kind, mime_data = build_bundle(available, lambda mime: mime_bytes[mime])
        assert kind == expected_kind

        clip_id = store.add(kind, mime_data)
        clip = store.all()[0]

        assert clip.id == clip_id
        assert clip.kind == expected_kind
        assert store.get_data(clip_id) == mime_data
    finally:
        store.close()
