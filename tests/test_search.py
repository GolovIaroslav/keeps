import time

import pytest

from keeps.search import CONTENT_LIMIT_BYTES, MatchReason, SearchIndex, remember_query


@pytest.fixture
def index():
    return SearchIndex()


def test_search_finds_text_beyond_preview_limit(index):
    content = ("x" * 400 + " hidden needle").encode()
    index.upsert(1, "text", {"text/plain": content})

    assert index.search("needle") == {1: MatchReason.EXACT}
    snippet = index.snippet(1, "needle", MatchReason.EXACT)
    assert snippet.startswith("…")
    assert "hidden needle" in snippet


def test_snippet_offsets_survive_casefold_expansion(index):
    content = ("ß" * 200 + " hidden needle").encode()
    index.upsert(1, "text", {"text/plain": content})

    snippet = index.snippet(1, "needle", MatchReason.EXACT)

    assert "hidden needle" in snippet


def test_search_requires_every_word_in_any_order(index):
    index.upsert(1, "text", {"text/plain": b"alpha middle beta"})
    index.upsert(2, "text", {"text/plain": b"alpha only"})

    assert index.search("BETA alpha") == {1: MatchReason.EXACT}


def test_search_is_cyrillic_case_insensitive(index):
    index.upsert(1, "text", {"text/plain": "Привет, Мир".encode()})

    assert index.search("ПРИВЕТ мир") == {1: MatchReason.EXACT}


def test_search_uses_decoded_file_names(index):
    index.upsert(
        1,
        "files",
        {"text/uri-list": b"file:///tmp/quarter%20report.pdf\nfile:///tmp/notes.txt"},
    )

    assert index.search("quarter report") == {1: MatchReason.EXACT}
    assert index.search("tmp") == {}


def test_search_reports_ocr_reason_and_allows_terms_across_fields(index):
    index.upsert(1, "image", {"image/png": b"png"}, "invoice number 42")
    index.upsert(2, "text", {"text/plain": b"invoice"}, "number 42")

    assert index.search("invoice 42") == {
        1: MatchReason.OCR,
        2: MatchReason.OCR,
    }
    assert index.snippet(1, "invoice", MatchReason.OCR) == "invoice number 42"


def test_search_only_indexes_first_ten_kib(index):
    content = b"x" * CONTENT_LIMIT_BYTES + b" outside-limit"
    index.upsert(1, "text", {"text/plain": content})

    assert index.search("outside-limit") == {}


def test_upsert_and_remove_keep_index_current(index):
    index.upsert(1, "text", {"text/plain": b"before"})
    index.upsert(1, "text", {"text/plain": b"after"})

    assert index.search("before") == {}
    assert index.search("after") == {1: MatchReason.EXACT}

    index.remove(1)
    assert index.search("after") == {}


def test_updating_ocr_keeps_content_and_replaces_ocr_text(index):
    index.upsert(1, "text", {"text/plain": b"plain field"}, "old scan")

    index.update_ocr(1, "new scan")

    assert index.search("plain") == {1: MatchReason.EXACT}
    assert index.search("old") == {}
    assert index.search("new") == {1: MatchReason.OCR}


def test_search_5000_ten_kib_documents_is_under_50ms():
    index = SearchIndex()
    padding = b"x" * (CONTENT_LIMIT_BYTES - 32)
    for clip_id in range(5000):
        content = f"document-{clip_id} ".encode() + padding
        index.upsert(clip_id, "text", {"text/plain": content})

    started = time.perf_counter()
    results = index.search("document-4242")
    elapsed = time.perf_counter() - started

    assert results == {4242: MatchReason.EXACT}
    assert elapsed < 0.05, f"full-content search took {elapsed * 1000:.1f}ms"


def test_remember_query_deduplicates_case_insensitively_and_caps_at_twenty():
    history = [f"query {index}" for index in range(20)]

    updated = remember_query(history, "  QUERY 5  ")

    assert updated[0] == "QUERY 5"
    assert updated.count("query 5") == 0
    assert len(updated) == 20


def test_remember_query_ignores_blank_query():
    assert remember_query(["kept"], "   ") == ["kept"]
