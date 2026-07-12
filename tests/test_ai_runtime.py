"""AiRuntime orchestration (PLAN.md §9/§9.2): real QCoreApplication +
QThreadPool + Signal marshalling, with fake embedder/OCR engine so no
onnxruntime/tokenizers/cv2 model weights are needed.

Only QtCore is imported here (QCoreApplication/QTimer/QThreadPool/Signal),
same as config.py's QSettings usage -- unlike QtGui/QtWidgets, it needs no
display/EGL and is safe on a headless CI runner (verified: `QCoreApplication`
starts fine with DISPLAY/WAYLAND_DISPLAY unset and no QT_QPA_PLATFORM).

Several of these are regression tests for the exact class of bug found live
in session 7: `_QuerySignals.finished` used to be `Signal(str, dict)`, which
PySide6 marshals as a string-keyed QVariantMap for the cross-thread queued
connection this needs -- our {int: float} payload silently became an empty
dict on every delivery, and no test caught it because nothing exercised the
real signal path (only `TextEmbedder.encode()` directly). These tests pump
the real Qt event loop so a regression here fails automatically instead of
requiring another live smoke test.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QSettings

from keeps.ai import models
from keeps.ai.runtime import AiRuntime, available_ocr_language_codes
from keeps.store import Store

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "3df40000000c4944415478da6360606000000004000160b3e1b40000000049"
    "454e44ae426082"
)
PNG_1X1_RED = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae426082"
)


class FakeEmbedder:
    """Deterministic, dependency-free stand-in for text_embed.TextEmbedder."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.is_loaded = True
        self.unloaded = False

    def encode(self, text: str) -> np.ndarray:
        self.calls.append(text)
        seed = sum(text.encode("utf-8")) or 1
        vec = np.array([seed % 7, seed % 11, seed % 13], dtype=np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm else vec

    def unload(self) -> None:
        self.unloaded = True


class FakeOcrEngine:
    """Deterministic, dependency-free stand-in for ocr.OcrEngine."""

    def __init__(self, text: str = "recognized text") -> None:
        self.text = text
        self.calls = 0
        self.unloaded = False

    def extract_text(self, png_bytes: bytes) -> str:
        self.calls += 1
        return self.text

    def unload(self) -> None:
        self.unloaded = True


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "keeps.db", max_items=500)
    yield s
    s.close()


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "keeps.ini"), QSettings.Format.IniFormat)


def _pump_until(qapp, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _settle(qapp, seconds: float = 0.1) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def _make_runtime(store, settings, *, rag_text=False, ocr=False, ocr_timing="delayed"):
    settings.setValue("ai/rag_text_enabled", rag_text)
    settings.setValue("ai/ocr_enabled", ocr)
    settings.setValue("ai/ocr_timing", ocr_timing)
    return AiRuntime(store, settings)


# -- text/html capture -> embedding (independent of OCR) ---------------------


def test_text_clip_gets_embedded_when_rag_enabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    fake = FakeEmbedder()
    runtime._text_embedder = fake

    clip_id = store.add("text", {"text/plain": b"hello world"})
    runtime.on_clip_captured(clip_id, "text")

    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))
    assert dict(store.get_all_embeddings(models.TEXT_EMBED.name)).keys() == {clip_id}
    assert fake.calls == ["hello world"]


def test_text_clip_not_embedded_when_rag_disabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=False)
    runtime._text_embedder = FakeEmbedder()

    clip_id = store.add("text", {"text/plain": b"hello world"})
    runtime.on_clip_captured(clip_id, "text")
    _settle(qapp)

    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


def test_files_clip_never_triggers_embed_or_ocr(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True, ocr=True, ocr_timing="immediate")
    runtime._text_embedder = FakeEmbedder()
    fake_ocr = FakeOcrEngine()
    runtime._ocr_engine = fake_ocr

    clip_id = store.add("files", {"text/uri-list": b"file:///a.txt"})
    runtime.on_clip_captured(clip_id, "files")
    _settle(qapp)

    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []
    assert fake_ocr.calls == 0


# -- image capture -> OCR, independently of RAG ------------------------------


def test_image_clip_ocr_immediate_sets_ocr_text(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine("Привет мир")

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    clip = next(c for c in store.all() if c.id == clip_id)
    assert clip.ocr_text == "Привет мир"


def test_image_clip_ignored_when_ocr_disabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=False)
    fake_ocr = FakeOcrEngine()
    runtime._ocr_engine = fake_ocr

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    _settle(qapp)

    assert fake_ocr.calls == 0
    assert clip_id in store.clips_missing_ocr()


def test_delayed_ocr_restarts_timer_and_batches_pending_ids(qapp, store, settings):
    settings.setValue("ai/ocr_delay_seconds", 0.2)
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="delayed")
    runtime._ocr_engine = FakeOcrEngine("text")

    clip_1 = store.add("image", {"image/png": PNG_1X1})
    clip_2 = store.add("image", {"image/png": PNG_1X1_RED})
    runtime.on_clip_captured(clip_1, "image")
    runtime.on_clip_captured(clip_2, "image")

    # Both must be pending in a single batch: the second capture has to
    # restart the debounce timer rather than letting the first one fire
    # alone (PLAN.md §9.2: "debounce от последнего", not a per-clip timer).
    assert runtime._pending_delayed_clip_ids == {clip_1, clip_2}

    assert _pump_until(qapp, lambda: not store.clips_missing_ocr())
    assert {c.id for c in store.all() if c.ocr_text} == {clip_1, clip_2}


# -- OCR + RAG together: OCR'd text is embedded too, independent of the
# (unimplemented) image_semantic_enabled toggle -- confirmed as intentional
# behavior by the user 2026-07-11, see PLAN.md §9. ---------------------------


def test_ocr_text_gets_embedded_when_rag_and_ocr_both_enabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine("screenshot text")
    fake_embedder = FakeEmbedder()
    runtime._text_embedder = fake_embedder

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))
    assert "screenshot text" in fake_embedder.calls


def test_ocr_text_not_embedded_when_rag_disabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=False, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine("screenshot text")

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    _settle(qapp)
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


def test_ocr_skips_embedding_for_blank_recognized_text(qapp, store, settings):
    # extract_text() can legitimately return "" (e.g. a screenshot with no
    # text) -- must not embed an empty string.
    runtime = _make_runtime(store, settings, rag_text=True, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine("   ")
    fake_embedder = FakeEmbedder()
    runtime._text_embedder = fake_embedder

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    _settle(qapp)
    assert fake_embedder.calls == []
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


# -- backlog sweeps (one-time pass when a toggle is first enabled) ----------


def test_text_embed_backlog_sweep_embeds_all_missing_clips(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    runtime._text_embedder = FakeEmbedder()
    ids = [store.add("text", {"text/plain": f"clip {i}".encode()}) for i in range(3)]

    runtime.run_text_embed_backlog_sweep()

    assert _pump_until(
        qapp, lambda: len(store.get_all_embeddings(models.TEXT_EMBED.name)) == len(ids)
    )


def test_text_embed_backlog_sweep_noop_when_rag_disabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=False)
    store.add("text", {"text/plain": b"clip"})

    runtime.run_text_embed_backlog_sweep()
    _settle(qapp)

    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


def test_ocr_backlog_sweep_ocrs_all_missing_image_clips(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=True)
    runtime._ocr_engine = FakeOcrEngine("x")
    store.add("image", {"image/png": PNG_1X1})
    store.add("image", {"image/png": PNG_1X1_RED})

    runtime.run_ocr_backlog_sweep()

    assert _pump_until(qapp, lambda: not store.clips_missing_ocr())


# -- async query scoring: the exact signal-marshalling regression class -----


def test_encode_query_async_delivers_nonempty_scores_across_thread_boundary(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    fake = FakeEmbedder()
    runtime._text_embedder = fake

    clip_id = store.add("text", {"text/plain": b"hello world"})
    vec_bytes = fake.encode("hello world").astype("float32").tobytes()
    store.set_embedding(clip_id, models.TEXT_EMBED.name, vec_bytes)

    received: dict = {}
    runtime.encode_query_async(
        "hello", lambda query, scores: received.update(query=query, scores=scores)
    )

    assert _pump_until(qapp, lambda: "scores" in received)
    assert received["query"] == "hello"
    assert isinstance(received["scores"], dict), "must survive the worker->main Qt signal intact"
    assert clip_id in received["scores"]
    assert isinstance(received["scores"][clip_id], float)


def test_encode_query_async_empty_query_short_circuits(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    received: dict = {}

    runtime.encode_query_async(
        "   ", lambda query, scores: received.update(query=query, scores=scores)
    )

    assert received == {"query": "   ", "scores": {}}


# -- OCR language selection (Ф9.6 PART 2) ------------------------------------


def test_available_ocr_language_codes_requires_detector_downloaded():
    # Every recognizer "downloaded", but the shared detector isn't -- nothing
    # can run without it.
    def is_downloaded_fn(spec):
        return spec is not models.OCR_DET

    assert available_ocr_language_codes(["eslav", "en"], is_downloaded_fn) == []


def test_available_ocr_language_codes_filters_unknown_and_not_downloaded():
    downloaded_recs = {"eslav"}

    def is_downloaded_fn(spec):
        if spec is models.OCR_DET:
            return True
        return any(spec is models.OCR_REC.get(code) for code in downloaded_recs)

    result = available_ocr_language_codes(["eslav", "en", "made-up-code"], is_downloaded_fn)

    assert result == ["eslav"]


def test_available_ocr_language_codes_preserves_requested_order():
    result = available_ocr_language_codes(["latin", "en", "ch"], lambda spec: True)

    assert result == ["latin", "en", "ch"]


def test_available_ocr_language_codes_empty_when_nothing_selected():
    assert available_ocr_language_codes([], lambda spec: True) == []


def test_get_ocr_engine_returns_none_when_nothing_downloaded(
    store, settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runtime = _make_runtime(store, settings)

    assert runtime._get_ocr_engine() is None


def test_load_ocr_engine_noop_when_nothing_downloaded(store, settings, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runtime = _make_runtime(store, settings)

    runtime.load_ocr_engine()  # must not raise despite no downloaded model


def test_ocr_status_not_downloaded_when_no_language_available(
    store, settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runtime = _make_runtime(store, settings)

    assert runtime.ocr_status() == models.ModelStatus.NOT_DOWNLOADED


def test_process_clip_ocr_noop_when_nothing_downloaded(
    qapp, store, settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="immediate")

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    _settle(qapp)

    assert clip_id in store.clips_missing_ocr()


def test_reset_ocr_engine_clears_cache_and_unloads(qapp, store, settings):
    runtime = _make_runtime(store, settings)
    fake = FakeOcrEngine()
    runtime._ocr_engine = fake

    runtime.reset_ocr_engine()

    assert fake.unloaded is True
    assert runtime._ocr_engine is None


# -- idle-unload (Model management, PLAN.md §9.1) ----------------------------


def test_idle_unload_unloads_when_past_threshold(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime._last_activity = time.monotonic() - 120  # 2 minutes ago

    runtime._check_idle_unload()

    assert fake.unloaded is True


def test_idle_unload_never_fires_when_minutes_is_zero(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 0)
    runtime = _make_runtime(store, settings)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime._last_activity = time.monotonic() - 10_000

    runtime._check_idle_unload()

    assert fake.unloaded is False
