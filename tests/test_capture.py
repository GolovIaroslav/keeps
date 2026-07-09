import pytest

from keeps.capture.base import (
    MIME_HTML,
    MIME_IMAGE,
    MIME_PLAIN,
    MIME_URI_LIST,
    SelfSetGuard,
    build_bundle,
    detect_kind,
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


def test_build_bundle_reads_only_relevant_mimes():
    reads = []

    def reader(mime: str) -> bytes:
        reads.append(mime)
        return b"payload-" + mime.encode()

    result = build_bundle({MIME_PLAIN, MIME_HTML, "text/rtf"}, reader)

    assert result is not None
    kind, mime_data = result
    assert kind == "html"
    assert reads == [MIME_HTML, MIME_PLAIN]
    assert mime_data == {
        MIME_HTML: b"payload-" + MIME_HTML.encode(),
        MIME_PLAIN: b"payload-" + MIME_PLAIN.encode(),
    }


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


def test_self_set_guard_skips_once_within_window():
    guard = SelfSetGuard(window_seconds=1.0)
    guard.mark_self_set()

    assert guard.consume_skip() is True
    assert guard.consume_skip() is False  # only the next event is skipped


def test_self_set_guard_no_skip_without_mark():
    guard = SelfSetGuard()
    assert guard.consume_skip() is False


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
