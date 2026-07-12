import sqlite3
import time

import pytest

from keeps import store as store_module
from keeps.search import MatchReason
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


def test_search_uses_full_content_and_reports_match_reason(store):
    clip_id = store.add("text", {"text/plain": (b"x" * 400) + b" hidden needle"})

    results, reasons = store.search_with_reasons("needle hidden")

    assert [clip.id for clip in results] == [clip_id]
    assert reasons == {clip_id: MatchReason.EXACT}


def test_search_index_tracks_content_updates_and_deletion(store):
    clip_id = store.add("text", {"text/plain": b"before"})

    store.update_content(clip_id, {"text/plain": b"after"})
    assert store.search("before") == []
    assert [clip.id for clip in store.search("after")] == [clip_id]

    store.delete(clip_id)
    assert store.search("after") == []


def test_search_index_rebuilds_from_persisted_full_content(tmp_path):
    db_path = tmp_path / "keeps.db"
    store = Store(db_path)
    clip_id = store.add("text", {"text/plain": (b"x" * 400) + b" persisted needle"})
    store.close()

    reopened = Store(db_path)
    results, reasons = reopened.search_with_reasons("persisted needle")
    snippet = reopened.search_snippet(clip_id, "needle", reasons[clip_id])
    reopened.close()

    assert [clip.id for clip in results] == [clip_id]
    assert "persisted needle" in snippet


def test_search_index_rebuild_handles_empty_file_list(tmp_path):
    db_path = tmp_path / "keeps.db"
    store = Store(db_path)
    clip_id = store.add("files", {"text/uri-list": b""})
    store.close()

    reopened = Store(db_path)
    clips = reopened.all()
    reopened.close()

    assert [clip.id for clip in clips] == [clip_id]


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


def test_update_content_replaces_data_and_preview(store):
    clip_id = store.add("text", {"text/plain": b"before"})
    old_hash = store.all()[0].hash

    result_id = store.update_content(clip_id, {"text/plain": b"after"})

    assert result_id == clip_id
    clip = store.all()[0]
    assert clip.preview == "after"
    assert clip.hash != old_hash
    assert store.get_data(clip_id) == {"text/plain": b"after"}


def test_update_content_bumps_to_top(store):
    a = store.add("text", {"text/plain": b"a"})
    time.sleep(0.002)
    store.add("text", {"text/plain": b"b"})
    time.sleep(0.002)

    store.update_content(a, {"text/plain": b"a-edited"})

    assert store.all()[0].id == a


def test_update_content_merges_into_existing_hash(store):
    keep = store.add("text", {"text/plain": b"target"})
    to_edit = store.add("text", {"text/plain": b"original"})

    result_id = store.update_content(to_edit, {"text/plain": b"target"})

    assert result_id == keep
    ids = {clip.id for clip in store.all()}
    assert to_edit not in ids, "edited clip must be dropped when it now duplicates another"
    assert keep in ids


def test_update_content_unknown_clip_raises(store):
    with pytest.raises(ValueError):
        store.update_content(999, {"text/plain": b"x"})


def test_search_matches_ocr_text(store):
    clip_id = store.add("image", {"image/png": PNG_1X1})
    store.set_ocr_text(clip_id, "распознанный текст на скриншоте")
    store.add("text", {"text/plain": b"unrelated"})

    results = store.search("скриншоте")
    assert len(results) == 1
    assert results[0].id == clip_id


def test_search_ocr_text_independent_of_preview_match(store):
    # A clip whose preview doesn't contain the query but whose ocr_text does
    # must still be found -- plain substring search always covers ocr_text
    # (PLAN.md §9), independent of any ai/* toggle.
    clip_id = store.add("image", {"image/png": PNG_1X1})
    store.set_ocr_text(clip_id, "invoice number 42")

    assert [c.id for c in store.search("invoice")] == [clip_id]


def test_set_embedding_and_get_all_embeddings(store):
    a = store.add("text", {"text/plain": b"first"})
    b = store.add("text", {"text/plain": b"second"})
    store.set_embedding(a, "model-x", b"vec-a")
    store.set_embedding(b, "model-x", b"vec-b")

    results = dict(store.get_all_embeddings("model-x"))
    assert results == {a: b"vec-a", b: b"vec-b"}
    assert store.get_all_embeddings("other-model") == []


def test_set_embedding_overwrites_on_conflict(store):
    clip_id = store.add("text", {"text/plain": b"x"})
    store.set_embedding(clip_id, "model-x", b"old-vec")
    store.set_embedding(clip_id, "model-y", b"new-vec")

    results = store.get_all_embeddings("model-y")
    assert results == [(clip_id, b"new-vec")]
    assert store.get_all_embeddings("model-x") == []


def test_clips_missing_ocr_lists_only_image_clips_without_ocr_text(store):
    with_ocr = store.add("image", {"image/png": PNG_1X1})
    store.set_ocr_text(with_ocr, "already scanned")
    other_png = PNG_1X1[:-1] + bytes([PNG_1X1[-1] ^ 0xFF])  # distinct hash, same valid header
    without_ocr = store.add("image", {"image/png": other_png})
    store.add("text", {"text/plain": b"not an image"})

    assert store.clips_missing_ocr() == [without_ocr]


def test_thumbnail_roundtrip_and_backlog_only_include_missing_images(store):
    with_thumbnail = store.add("image", {"image/png": PNG_1X1})
    other_png = PNG_1X1[:-1] + bytes([PNG_1X1[-1] ^ 0xFF])
    without_thumbnail = store.add("image", {"image/png": other_png})
    store.add("text", {"text/plain": b"not an image"})

    source_hash, source_png = store.get_thumbnail_source(with_thumbnail)
    assert source_png == PNG_1X1
    assert source_hash == next(clip.hash for clip in store.all() if clip.id == with_thumbnail)
    assert store.get_thumbnail(with_thumbnail) is None
    assert store.clips_missing_thumbnail() == [with_thumbnail, without_thumbnail]

    assert store.set_thumbnail(with_thumbnail, source_hash, b"small-png") is True

    assert store.get_thumbnail(with_thumbnail) == b"small-png"
    assert store.clips_missing_thumbnail() == [without_thumbnail]


def test_thumbnail_write_after_clip_deletion_is_ignored(store):
    clip_id = store.add("image", {"image/png": PNG_1X1})
    source_hash, _ = store.get_thumbnail_source(clip_id)
    store.delete(clip_id)

    assert store.set_thumbnail(clip_id, source_hash, b"late-worker-result") is False
    assert store.get_thumbnail(clip_id) is None


def test_deleting_clip_cascades_to_thumbnail(store):
    clip_id = store.add("image", {"image/png": PNG_1X1})
    source_hash, _ = store.get_thumbnail_source(clip_id)
    store.set_thumbnail(clip_id, source_hash, b"small-png")

    store.delete(clip_id)

    assert store.get_thumbnail(clip_id) is None


def test_updating_image_content_invalidates_thumbnail(store):
    clip_id = store.add("image", {"image/png": PNG_1X1})
    old_hash, _ = store.get_thumbnail_source(clip_id)
    store.set_thumbnail(clip_id, old_hash, b"thumbnail-for-old-image")
    edited_png = PNG_1X1[:-1] + bytes([PNG_1X1[-1] ^ 0xFF])

    store.update_content(clip_id, {"image/png": edited_png})

    assert store.set_thumbnail(clip_id, old_hash, b"late-old-thumbnail") is False
    assert store.get_thumbnail(clip_id) is None
    assert store.clips_missing_thumbnail() == [clip_id]


def test_clips_missing_embedding_lists_only_text_and_html_without_it(store):
    embedded = store.add("text", {"text/plain": b"already embedded"})
    store.set_embedding(embedded, "model-x", b"vec")
    not_embedded = store.add("html", {"text/plain": b"needs embedding", "text/html": b"<p>x</p>"})
    store.add("image", {"image/png": PNG_1X1})  # never text-embedded, must be excluded

    assert store.clips_missing_embedding("model-x") == [not_embedded]


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


# -- Ф10: migrations + backups + DB maintenance ------------------------------


def test_fresh_db_reaches_latest_version_with_no_backup(tmp_path):
    db_path = tmp_path / "keeps.db"
    Store(db_path, max_items=500).close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert version == store_module.LATEST_VERSION
    assert list(tmp_path.glob("*.backup-*")) == []


def test_pre_migration_db_gets_stamped_without_losing_data_or_backing_up(tmp_path):
    """Simulates a DB created by pre-Ф10 code: tables exist (CREATE TABLE IF
    NOT EXISTS was always run) but user_version was never touched, so
    SQLite's default of 0 is still there.
    """
    db_path = tmp_path / "keeps.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(store_module.SCHEMA)
    conn.execute(
        "INSERT INTO clips (created_at, last_used_at, kind, preview, hash, use_count) "
        "VALUES (0, 0, 'text', 'pre-existing clip', 'h', 1)"
    )
    conn.commit()
    conn.close()

    s = Store(db_path, max_items=500)
    clips = s.all()
    s.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()

    assert [c.preview for c in clips] == ["pre-existing clip"]
    assert version == 1
    assert list(tmp_path.glob("*.backup-*")) == []


def test_migration_backs_up_first_then_preserves_data_and_bumps_version(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "keeps.db"
    s = Store(db_path, max_items=500)
    s.add("text", {"text/plain": b"before migration"})
    s.close()

    def add_dummy_column(conn: sqlite3.Connection) -> None:
        conn.execute("ALTER TABLE clips ADD COLUMN dummy TEXT")

    monkeypatch.setattr(store_module, "LATEST_VERSION", 2)
    monkeypatch.setattr(store_module, "MIGRATIONS", {2: add_dummy_column})

    s2 = Store(db_path, max_items=500)
    clips = s2.all()
    s2.close()

    conn = sqlite3.connect(db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    columns = [row[1] for row in conn.execute("PRAGMA table_info(clips)")]
    conn.close()

    assert [c.preview for c in clips] == ["before migration"]
    assert version == 2
    assert "dummy" in columns
    backups = list(tmp_path.glob("keeps.db.backup-*"))
    assert len(backups) == 1


def test_backup_now_creates_a_restorable_copy(tmp_path):
    s = Store(tmp_path / "keeps.db", max_items=500)
    s.add("text", {"text/plain": b"hello"})

    backup_path = s.backup_now()
    s.close()

    assert backup_path.exists()
    assert backup_path.name.startswith("keeps.db.backup-")

    restored = Store(backup_path, max_items=500)
    clips = restored.all()
    restored.close()
    assert [c.preview for c in clips] == ["hello"]


def test_rotate_backups_keeps_only_newest_three(tmp_path):
    db_path = tmp_path / "keeps.db"
    db_path.touch()
    names = [f"keeps.db.backup-2026010{i}-000000" for i in range(1, 6)]
    for name in names:
        (tmp_path / name).touch()

    store_module._rotate_backups(db_path)

    remaining = sorted(p.name for p in tmp_path.glob("keeps.db.backup-*"))
    assert remaining == names[-3:]


def test_compact_shrinks_file_after_bulk_delete(tmp_path):
    s = Store(tmp_path / "keeps.db", max_items=100_000)
    for i in range(500):
        s.add("text", {"text/plain": (f"clip {i} " * 200).encode()})
    s.clear_history(include_pinned=True)

    before, after = s.compact()
    s.close()
    assert after < before


def test_clear_history_default_spares_pinned(store):
    unpinned_id = store.add("text", {"text/plain": b"a"})
    pinned_id = store.add("text", {"text/plain": b"b"})
    store.set_pinned(pinned_id, True)

    deleted = store.clear_history()

    assert deleted == 1
    remaining_ids = [c.id for c in store.all()]
    assert remaining_ids == [pinned_id]
    assert unpinned_id not in remaining_ids


def test_clear_history_include_pinned_deletes_everything(store):
    store.add("text", {"text/plain": b"a"})
    pinned_id = store.add("text", {"text/plain": b"b"})
    store.set_pinned(pinned_id, True)

    deleted = store.clear_history(include_pinned=True)

    assert deleted == 2
    assert store.all() == []
