import time

import pytest

from keeps.store import Store, build_preview, normalize

TEXT_KINDS = {
    "text": {"text/plain": b"hello world"},
    "html": {
        "text/plain": b"hello world",
        "text/html": b"<b>hello world</b>",
    },
    "files": {"text/uri-list": b"file:///a.txt\nfile:///b.txt"},
}

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "3df40000000c4944415478da6360606000000004000160b3e1b40000000049"
    "454e44ae426082"
)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "keeps.db", max_items=500)
    yield s
    s.close()


@pytest.mark.parametrize("kind,mime_data", list(TEXT_KINDS.items()))
def test_roundtrip_all_kinds(store, kind, mime_data):
    clip_id = store.add(kind, mime_data)
    stored = store.get_data(clip_id)
    for mime, data in mime_data.items():
        assert stored[mime] == data
    clip = store.all()[0]
    assert clip.kind == kind
    assert clip.id == clip_id


def test_roundtrip_image(store):
    mime_data = {"image/png": PNG_1X1}
    clip_id = store.add("image", mime_data)
    assert store.get_data(clip_id)["image/png"] == PNG_1X1
    assert store.all()[0].preview == "[image 1x1]"


def test_dedup_move_to_top(store):
    first = store.add("text", {"text/plain": b"first"})
    time.sleep(0.002)
    store.add("text", {"text/plain": b"second"})
    time.sleep(0.002)
    again_id = store.add("text", {"text/plain": b"first"})

    assert again_id == first
    clips = store.all()
    assert len(clips) == 2, "duplicate must not create a second row"
    assert clips[0].id == first
    assert clips[0].use_count == 2


def test_trim_keeps_pinned(store):
    store.max_items = 2
    a = store.add("text", {"text/plain": b"a"})
    store.set_pinned(a, True)
    store.add("text", {"text/plain": b"b"})
    store.add("text", {"text/plain": b"c"})
    store.add("text", {"text/plain": b"d"})
    store.trim()

    ids = {clip.id for clip in store.all()}
    assert a in ids, "pinned clip must survive trim even if old"
    assert len(store.all()) == 3, "pinned clip plus max_items unpinned clips"


@pytest.mark.parametrize(
    "needle,haystack,expected",
    [
        ("hello", "Hello World", True),
        ("ПРИВЕТ", "привет мир", True),
        ("Ёлка", "ёлка", True),
        ("xyz", "hello world", False),
    ],
)
def test_normalize_case_insensitive(needle, haystack, expected):
    assert (normalize(needle) in normalize(haystack)) == expected


def test_search_cyrillic_case_insensitive(store):
    store.add("text", {"text/plain": "Привет, Мир".encode()})
    store.add("text", {"text/plain": b"unrelated"})

    results = store.search("привет")
    assert len(results) == 1
    assert "Привет" in results[0].preview


def test_hash_stable_across_instances(tmp_path):
    s1 = Store(tmp_path / "keeps.db")
    clip_id = s1.add("text", {"text/plain": b"stable content"})
    h1 = s1.all()[0].hash
    s1.close()

    s2 = Store(tmp_path / "keeps.db")
    dup_id = s2.add("text", {"text/plain": b"stable content"})
    assert dup_id == clip_id
    assert s2.all()[0].hash == h1
    s2.close()


def test_build_preview_truncates_long_text():
    long_text = "x" * 1000
    preview = build_preview("text", {"text/plain": long_text.encode()})
    assert len(preview) == 300


def test_add_and_search_5000_records_is_fast(tmp_path):
    store = Store(tmp_path / "keeps.db", max_items=10_000)
    start = time.perf_counter()
    for i in range(5000):
        store.add("text", {"text/plain": f"clip number {i}".encode()})
    add_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    results = store.search("number 4242")
    search_elapsed = time.perf_counter() - start

    store.close()
    assert len(results) == 1
    assert search_elapsed < 0.05, f"search took {search_elapsed * 1000:.1f}ms"
    assert add_elapsed < 10, f"add took {add_elapsed:.2f}s"
