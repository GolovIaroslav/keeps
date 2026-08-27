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

import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QSettings

from keeps.ai import models
from keeps.ai.ranking import SearchMode
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
        self.is_loaded = False


class FakeOcrEngine:
    """Deterministic, dependency-free stand-in for ocr.OcrEngine."""

    def __init__(self, text: str = "recognized text") -> None:
        self.text = text
        self.calls = 0
        self.is_loaded = True
        self.unloaded = False

    def load(self) -> None:
        self.is_loaded = True

    def extract_text(self, png_bytes: bytes) -> str:
        self.calls += 1
        return self.text

    def unload(self) -> None:
        self.unloaded = True
        self.is_loaded = False


class FakeImageEmbedder(FakeEmbedder):
    def encode_text(self, text: str) -> np.ndarray:
        return self.encode(text)

    def encode_image(self, image_bytes: bytes) -> np.ndarray:
        self.calls.append("<image>")
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


class BlockingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def encode(self, text: str) -> np.ndarray:
        self.started.set()
        self.release.wait(timeout=5)
        return super().encode(text)


class BlockingOcrEngine(FakeOcrEngine):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.started = threading.Event()
        self.release = threading.Event()

    def extract_text(self, png_bytes: bytes) -> str:
        self.started.set()
        self.release.wait(timeout=5)
        return super().extract_text(png_bytes)


class BlockingImageEmbedder(FakeImageEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def encode_image(self, image_bytes: bytes) -> np.ndarray:
        self.started.set()
        self.release.wait(timeout=5)
        return super().encode_image(image_bytes)


class BlockingQueryImageEmbedder(FakeImageEmbedder):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def encode_text(self, text: str) -> np.ndarray:
        self.started.set()
        self.release.wait(timeout=5)
        return super().encode_text(text)


class FailingImageEmbedder(FakeImageEmbedder):
    def encode_image(self, image_bytes: bytes) -> np.ndarray:
        self.calls.append("<image-failed>")
        raise ValueError("invalid image payload")


class FailingTextEmbedder(FakeEmbedder):
    def encode(self, text: str) -> np.ndarray:
        self.calls.append(text)
        raise ValueError("text model failure")


class FailingOcrEngine(FakeOcrEngine):
    def extract_text(self, png_bytes: bytes) -> str:
        self.calls += 1
        raise ValueError("ocr model failure")


class FailingQueryImageEmbedder(FakeImageEmbedder):
    def encode_text(self, text: str) -> np.ndarray:
        raise ValueError("visual query model failure")


class ContentAwareBlockingOcrEngine(FakeOcrEngine):
    def __init__(self) -> None:
        super().__init__("")
        self.started = threading.Event()
        self.release = threading.Event()

    def extract_text(self, png_bytes: bytes) -> str:
        self.started.set()
        self.release.wait(timeout=5)
        self.calls += 1
        return "new pixels" if png_bytes == PNG_1X1_RED else "old pixels"


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


def _make_runtime(
    store,
    settings,
    *,
    rag_text=False,
    ocr=False,
    image_semantic=False,
    ocr_timing="delayed",
):
    settings.setValue("ai/rag_text_enabled", rag_text)
    settings.setValue("ai/ocr_enabled", ocr)
    settings.setValue("ai/image_semantic_enabled", image_semantic)
    settings.setValue("ai/ocr_timing", ocr_timing)
    return AiRuntime(store, settings)


def test_background_ai_pool_is_bounded_for_model_memory(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True, ocr=True)

    assert runtime.ai_task_max_threads == 1


def test_keyword_search_is_the_default_mode(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)

    assert runtime.search_mode == SearchMode.KEYWORD


# -- text/html capture -> embedding (independent of OCR) ---------------------


def test_text_clip_gets_embedded_when_rag_enabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime.set_search_mode(SearchMode.BLENDED)

    clip_id = store.add("text", {"text/plain": b"hello world"})
    runtime.on_clip_captured(clip_id, "text")

    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))
    assert dict(store.get_all_embeddings(models.TEXT_EMBED.name)).keys() == {clip_id}
    assert fake.calls == ["hello world"]


def test_duplicate_text_capture_does_not_repeat_completed_embedding(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime.set_search_mode(SearchMode.BLENDED)

    clip_id = store.add("text", {"text/plain": b"same text"})
    runtime.on_clip_captured(clip_id, "text")
    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))

    runtime.on_clip_captured(clip_id, "text")
    _settle(qapp)

    assert fake.calls == ["same text"]


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


def test_image_clip_gets_visual_embedding_when_semantic_mode_is_active(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, image_semantic=True)
    runtime._image_embedder = FakeImageEmbedder()
    runtime.set_search_mode(SearchMode.SEMANTIC)

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(
        qapp, lambda: bool(store.get_all_embeddings(models.IMAGE_EMBED.name))
    )
    assert store.get_all_embeddings(models.IMAGE_EMBED.name)[0][0] == clip_id


def test_query_fuses_text_and_visual_spaces_without_comparing_cosines(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, rag_text=True, image_semantic=True)
    text_embedder = FakeEmbedder()
    image_embedder = FakeImageEmbedder()
    runtime._text_embedder = text_embedder
    runtime._image_embedder = image_embedder
    runtime.set_search_mode(SearchMode.SEMANTIC)
    assert _pump_until(qapp, lambda: not runtime._pending_ai_tasks)

    text_id = store.add("text", {"text/plain": b"document"})
    image_id = store.add("image", {"image/png": PNG_1X1})
    store.set_embedding(
        text_id,
        models.TEXT_EMBED.name,
        text_embedder.encode("horse").astype("float32").tobytes(),
    )
    store.set_embedding(
        image_id,
        models.IMAGE_EMBED.name,
        image_embedder.encode_text("horse").astype("float32").tobytes(),
    )
    completed = []

    runtime.encode_query_async("horse", lambda query, scores: completed.append(scores))

    assert _pump_until(qapp, lambda: bool(completed))
    assert completed == [{text_id: 1.0, image_id: 1.0}]


def test_duplicate_image_capture_does_not_repeat_completed_ocr(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="immediate")
    fake_ocr = FakeOcrEngine("recognized once")
    runtime._ocr_engine = fake_ocr

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())

    # Spectacle can publish the same screenshot again after Keeps has already
    # indexed it. Store.add() returns the existing clip id in that case.
    runtime.on_clip_captured(clip_id, "image")
    _settle(qapp)

    assert fake_ocr.calls == 1


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


# -- OCR + RAG together: OCR'd text is embedded independently of visual
# image-semantic indexing, so an image may retain both model vectors. -------


def test_ocr_text_gets_embedded_when_rag_and_ocr_both_enabled(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine("screenshot text")
    fake_embedder = FakeEmbedder()
    runtime._text_embedder = fake_embedder
    runtime.set_search_mode(SearchMode.BLENDED)

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
    runtime.set_search_mode(SearchMode.BLENDED)

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    _settle(qapp)
    assert fake_embedder.calls == []
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


# -- backlog sweeps (one-time pass when a toggle is first enabled) ----------


def test_selecting_semantic_mode_embeds_backlog_but_keyword_mode_does_not(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, rag_text=True)
    runtime._text_embedder = FakeEmbedder()
    ids = [store.add("text", {"text/plain": f"clip {i}".encode()}) for i in range(3)]

    for clip_id in ids:
        runtime.on_clip_captured(clip_id, "text")
    runtime.run_text_embed_backlog_sweep()
    _settle(qapp)
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []

    runtime.set_search_mode(SearchMode.BLENDED)

    assert _pump_until(
        qapp, lambda: len(store.get_all_embeddings(models.TEXT_EMBED.name)) == len(ids)
    )


def test_ocr_created_in_keyword_mode_is_embedded_after_semantic_activation(
    qapp, store, settings
):
    runtime = _make_runtime(
        store, settings, rag_text=True, ocr=True, ocr_timing="immediate"
    )
    runtime._ocr_engine = FakeOcrEngine("searchable screenshot")
    fake_embedder = FakeEmbedder()
    runtime._text_embedder = fake_embedder

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []

    runtime.set_search_mode(SearchMode.SEMANTIC)

    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))
    assert fake_embedder.calls == ["searchable screenshot"]


def test_returning_to_keyword_cancels_queued_semantic_backlog(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    blocker = BlockingEmbedder()
    runtime._text_embedder = blocker
    for index in range(4):
        store.add("text", {"text/plain": f"clip {index}".encode()})

    runtime.set_search_mode(SearchMode.BLENDED)
    assert blocker.started.wait(timeout=2)
    runtime.set_search_mode(SearchMode.KEYWORD)
    blocker.release.set()

    assert _pump_until(qapp, lambda: not runtime._pending_ai_tasks)
    assert blocker.calls == ["clip 0"]
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


def test_returning_to_keyword_cancels_inflight_semantic_query(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    blocker = BlockingEmbedder()
    runtime._text_embedder = blocker
    callbacks = []
    runtime.set_search_mode(SearchMode.SEMANTIC)

    runtime.encode_query_async("needle", lambda *result: callbacks.append(result))
    assert blocker.started.wait(timeout=2)
    runtime.set_search_mode(SearchMode.KEYWORD)
    blocker.release.set()
    _settle(qapp)

    assert blocker.calls == ["needle"]
    assert callbacks == []


def test_completed_backlog_emits_one_semantic_index_change(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    runtime._text_embedder = FakeEmbedder()
    for index in range(3):
        store.add("text", {"text/plain": f"clip {index}".encode()})
    changes = []
    runtime.semantic_index_changed.connect(lambda: changes.append(True))

    runtime.set_search_mode(SearchMode.BLENDED)

    assert _pump_until(qapp, lambda: len(changes) == 1)
    assert len(store.get_all_embeddings(models.TEXT_EMBED.name)) == 3


def test_image_backlog_refreshes_semantic_results_progressively(qapp, store, settings):
    runtime = _make_runtime(store, settings, image_semantic=True)
    runtime._image_embedder = FakeImageEmbedder()
    for index in range(17):
        # Different bytes avoid Store's content-hash dedup while the fake
        # embedder deliberately ignores image validity.
        store.add("image", {"image/png": PNG_1X1 + bytes([index])})
    changes = []
    runtime.semantic_index_changed.connect(lambda: changes.append(True))

    runtime.set_search_mode(SearchMode.BLENDED)

    assert _pump_until(qapp, lambda: not runtime._pending_ai_tasks, timeout=3)
    assert len(store.get_all_embeddings(models.IMAGE_EMBED.name)) == 17
    assert len(changes) >= 2


def test_failed_visual_embedding_does_not_wedge_backlog(qapp, store, settings):
    runtime = _make_runtime(store, settings, image_semantic=True)
    runtime._image_embedder = FailingImageEmbedder()
    first = store.add("image", {"image/png": PNG_1X1})
    second = store.add("image", {"image/png": PNG_1X1_RED})

    runtime.set_search_mode(SearchMode.SEMANTIC)

    assert _pump_until(
        qapp,
        lambda: not runtime._pending_ai_tasks and not runtime._image_embed_backlog,
    )
    assert store.get_all_embeddings(models.IMAGE_EMBED.name) == []
    assert set(store.clips_missing_image_embedding(models.IMAGE_EMBED.name)) == {
        first,
        second,
    }


def test_failed_text_embedding_does_not_wedge_backlog(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    runtime._text_embedder = FailingTextEmbedder()
    first = store.add("text", {"text/plain": b"first"})
    second = store.add("text", {"text/plain": b"second"})

    runtime.set_search_mode(SearchMode.SEMANTIC)

    assert _pump_until(
        qapp,
        lambda: not runtime._pending_ai_tasks and not runtime._text_embed_backlog,
    )
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []
    assert set(store.clips_missing_embedding(models.TEXT_EMBED.name)) == {first, second}


def test_failed_ocr_does_not_wedge_backlog(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=True)
    runtime._ocr_engine = FailingOcrEngine()
    first = store.add("image", {"image/png": PNG_1X1})
    second = store.add("image", {"image/png": PNG_1X1_RED})

    runtime.run_ocr_backlog_sweep()

    assert _pump_until(
        qapp,
        lambda: not runtime._pending_ai_tasks and not runtime._ocr_backlog,
    )
    assert set(store.clips_missing_ocr()) == {first, second}


def test_large_backlogs_keep_only_one_payload_per_ai_kind_in_qt_pool(
    qapp, store, settings
):
    runtime = _make_runtime(
        store, settings, rag_text=True, ocr=True, image_semantic=True
    )
    text = BlockingEmbedder()
    image = BlockingImageEmbedder()
    ocr = BlockingOcrEngine("ocr")
    runtime._text_embedder = text
    runtime._image_embedder = image
    runtime._ocr_engine = ocr
    for index in range(12):
        store.add("text", {"text/plain": f"large text {index}".encode()})
        store.add("image", {"image/png": PNG_1X1 + bytes([index])})

    runtime.set_search_mode(SearchMode.BLENDED)
    runtime.run_ocr_backlog_sweep()
    assert text.started.wait(timeout=2)

    assert sum(kind == "text" for kind, _ in runtime._pending_ai_tasks) <= 1
    assert sum(kind == "image" for kind, _ in runtime._pending_ai_tasks) <= 1
    assert sum(kind == "ocr" for kind, _ in runtime._pending_ai_tasks) <= 1
    assert len(runtime._text_embed_backlog) >= 10
    assert len(runtime._image_embed_backlog) >= 10
    assert len(runtime._ocr_backlog) >= 10

    text.release.set()
    image.release.set()
    ocr.release.set()
    assert _pump_until(
        qapp,
        lambda: not runtime._pending_ai_tasks
        and not runtime._text_embed_backlog
        and not runtime._image_embed_backlog
        and not runtime._ocr_backlog,
        timeout=5,
    )


def test_text_edit_during_inflight_embedding_cannot_store_stale_vector(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, rag_text=True)
    blocker = BlockingEmbedder()
    runtime._text_embedder = blocker
    runtime.set_search_mode(SearchMode.SEMANTIC)
    clip_id = store.add("text", {"text/plain": b"old content"})
    runtime.on_clip_captured(clip_id, "text")
    assert blocker.started.wait(timeout=2)

    store.update_content(clip_id, {"text/plain": b"new content"})
    runtime.on_clip_captured(clip_id, "text")
    blocker.release.set()

    assert _pump_until(
        qapp,
        lambda: bool(store.get_all_embeddings(models.TEXT_EMBED.name))
        and not runtime._pending_ai_tasks,
    )
    expected = FakeEmbedder().encode("new content").astype("float32").tobytes()
    assert dict(store.get_all_embeddings(models.TEXT_EMBED.name))[clip_id] == expected
    assert blocker.calls == ["old content", "new content"]


def test_image_edit_during_inflight_visual_embedding_cannot_store_stale_vector(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, image_semantic=True)
    blocker = BlockingImageEmbedder()
    runtime._image_embedder = blocker
    runtime.set_search_mode(SearchMode.SEMANTIC)
    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert blocker.started.wait(timeout=2)

    store.update_content(clip_id, {"image/png": PNG_1X1_RED})
    runtime.on_clip_captured(clip_id, "image")
    blocker.release.set()

    assert _pump_until(
        qapp,
        lambda: bool(store.get_all_embeddings(models.IMAGE_EMBED.name))
        and not runtime._pending_ai_tasks,
    )
    assert blocker.calls == ["<image>", "<image>"]


def test_image_edit_during_inflight_ocr_cannot_restore_old_ocr_text(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="immediate")
    blocker = ContentAwareBlockingOcrEngine()
    runtime._ocr_engine = blocker
    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert blocker.started.wait(timeout=2)

    store.update_content(clip_id, {"image/png": PNG_1X1_RED})
    runtime.on_clip_captured(clip_id, "image")
    blocker.release.set()

    assert _pump_until(qapp, lambda: not runtime._pending_ai_tasks)
    clip = next(c for c in store.all() if c.id == clip_id)
    assert clip.ocr_text == "new pixels"
    assert blocker.calls == 2


def test_ocr_finishing_after_semantic_activation_gets_embedded(qapp, store, settings):
    runtime = _make_runtime(
        store, settings, rag_text=True, ocr=True, ocr_timing="immediate"
    )
    ocr = BlockingOcrEngine("late OCR text")
    runtime._ocr_engine = ocr
    embedder = FakeEmbedder()
    runtime._text_embedder = embedder

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert ocr.started.wait(timeout=2)
    runtime.set_search_mode(SearchMode.SEMANTIC)
    ocr.release.set()

    assert _pump_until(qapp, lambda: store.get_all_embeddings(models.TEXT_EMBED.name))
    assert embedder.calls == ["late OCR text"]


def test_ocr_finishing_after_return_to_keyword_is_not_embedded(qapp, store, settings):
    runtime = _make_runtime(
        store, settings, rag_text=True, ocr=True, ocr_timing="immediate"
    )
    ocr = BlockingOcrEngine("late OCR text")
    runtime._ocr_engine = ocr
    embedder = FakeEmbedder()
    runtime._text_embedder = embedder
    runtime.set_search_mode(SearchMode.SEMANTIC)

    clip_id = store.add("image", {"image/png": PNG_1X1})
    runtime.on_clip_captured(clip_id, "image")
    assert ocr.started.wait(timeout=2)
    runtime.set_search_mode(SearchMode.KEYWORD)
    ocr.release.set()

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    _settle(qapp)
    assert embedder.calls == []
    assert store.get_all_embeddings(models.TEXT_EMBED.name) == []


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
    runtime.set_search_mode(SearchMode.SEMANTIC)

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


def test_failed_visual_query_encoder_keeps_text_results_and_delivers_callback(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, rag_text=True, image_semantic=True)
    text = FakeEmbedder()
    runtime._text_embedder = text
    runtime._image_embedder = FailingQueryImageEmbedder()
    runtime.set_search_mode(SearchMode.SEMANTIC)
    clip_id = store.add("text", {"text/plain": b"hello"})
    store.set_embedding(
        clip_id,
        models.TEXT_EMBED.name,
        text.encode("hello").astype("float32").tobytes(),
    )
    received = []

    runtime.encode_query_async("hello", lambda query, scores: received.append(scores))

    assert _pump_until(qapp, lambda: bool(received))
    assert clip_id in received[0]


def test_failed_only_query_encoder_still_delivers_empty_callback(qapp, store, settings):
    runtime = _make_runtime(store, settings, image_semantic=True)
    runtime._image_embedder = FailingQueryImageEmbedder()
    runtime.set_search_mode(SearchMode.SEMANTIC)
    received = []

    runtime.encode_query_async("hello", lambda query, scores: received.append(scores))

    assert _pump_until(qapp, lambda: bool(received))
    assert received == [{}]


def test_encode_query_async_empty_query_short_circuits(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    received: dict = {}

    runtime.encode_query_async(
        "   ", lambda query, scores: received.update(query=query, scores=scores)
    )

    assert received == {"query": "   ", "scores": {}}


def test_new_query_cancels_stale_inflight_semantic_result(qapp, store, settings):
    runtime = _make_runtime(store, settings, rag_text=True)
    blocker = BlockingEmbedder()
    runtime._text_embedder = blocker
    runtime.set_search_mode(SearchMode.SEMANTIC)
    clip_id = store.add("text", {"text/plain": b"document"})
    vector = FakeEmbedder().encode("new").astype("float32").tobytes()
    store.set_embedding(clip_id, models.TEXT_EMBED.name, vector)
    completed = []

    runtime.encode_query_async("old", lambda query, scores: completed.append(query))
    assert blocker.started.wait(timeout=2)
    runtime.encode_query_async("new", lambda query, scores: completed.append(query))
    blocker.release.set()

    assert _pump_until(qapp, lambda: completed == ["new"])


def test_rapid_queries_coalesce_embedding_snapshots(
    qapp, store, settings, monkeypatch
):
    runtime = _make_runtime(store, settings, rag_text=True)
    blocker = BlockingEmbedder()
    runtime._text_embedder = blocker
    runtime.set_search_mode(SearchMode.SEMANTIC)
    clip_id = store.add("text", {"text/plain": b"document"})
    vector = FakeEmbedder().encode("latest").astype("float32").tobytes()
    store.set_embedding(clip_id, models.TEXT_EMBED.name, vector)

    original_get_all = store.get_all_embeddings
    snapshot_calls = []

    def counted_get_all(model_name):
        snapshot_calls.append(model_name)
        return original_get_all(model_name)

    monkeypatch.setattr(store, "get_all_embeddings", counted_get_all)
    completed = []

    runtime.encode_query_async("old", lambda query, scores: completed.append(query))
    assert blocker.started.wait(timeout=2)
    for index in range(10):
        query = "latest" if index == 9 else f"stale-{index}"
        runtime.encode_query_async(query, lambda query, scores: completed.append(query))

    # Only the running query owns a materialized embedding snapshot. The ten
    # newer requests are represented by one replaceable lightweight request.
    assert snapshot_calls == [models.TEXT_EMBED.name]

    blocker.release.set()

    assert _pump_until(qapp, lambda: completed == ["latest"])
    assert snapshot_calls == [models.TEXT_EMBED.name, models.TEXT_EMBED.name]


def test_disabling_visual_semantics_cancels_inflight_mixed_query(
    qapp, store, settings
):
    runtime = _make_runtime(store, settings, rag_text=True, image_semantic=True)
    text = FakeEmbedder()
    visual = BlockingQueryImageEmbedder()
    runtime._text_embedder = text
    runtime._image_embedder = visual
    runtime.set_search_mode(SearchMode.SEMANTIC)

    text_id = store.add("text", {"text/plain": b"document"})
    image_id = store.add("image", {"image/png": PNG_1X1})
    store.set_embedding(
        text_id,
        models.TEXT_EMBED.name,
        text.encode("needle").astype("float32").tobytes(),
    )
    store.set_embedding(
        image_id,
        models.IMAGE_EMBED.name,
        FakeImageEmbedder().encode_text("needle").astype("float32").tobytes(),
    )
    completed = []

    runtime.encode_query_async(
        "old mixed query", lambda query, scores: completed.append((query, scores))
    )
    assert visual.started.wait(timeout=2)

    settings.setValue("ai/image_semantic_enabled", False)
    runtime.semantic_capabilities_changed()
    runtime.encode_query_async(
        "needle", lambda query, scores: completed.append((query, scores))
    )
    visual.release.set()

    assert _pump_until(qapp, lambda: len(completed) == 1)
    query, scores = completed[0]
    assert query == "needle"
    assert text_id in scores
    assert image_id not in scores


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


def test_idle_unload_unloads_ocr_engine_when_past_threshold(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    fake = FakeOcrEngine()
    runtime._ocr_engine = fake
    runtime._last_activity = time.monotonic() - 120  # 2 minutes ago

    runtime._check_idle_unload()

    assert fake.unloaded is True
    assert runtime._ocr_engine is None


def test_idle_unload_checks_both_models_in_one_pass(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    fake_embedder = FakeEmbedder()
    fake_ocr = FakeOcrEngine()
    runtime._text_embedder = fake_embedder
    runtime._ocr_engine = fake_ocr
    runtime._last_activity = time.monotonic() - 120  # 2 minutes ago

    runtime._check_idle_unload()

    assert fake_embedder.unloaded is True
    assert fake_ocr.unloaded is True


def test_idle_unload_releases_native_heap_after_models_unload(
    qapp, store, settings, monkeypatch
):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    runtime._text_embedder = FakeEmbedder()
    runtime._last_activity = time.monotonic() - 120
    released = []
    monkeypatch.setattr(
        "keeps.ai.runtime._release_unused_heap_memory", lambda: released.append(True)
    )

    runtime._check_idle_unload()

    assert released == [True]


def test_idle_unload_waits_for_active_ai_work(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime._last_activity = time.monotonic() - 120

    class BusyPool:
        def activeThreadCount(self):
            return 1

    runtime._ai_pool = BusyPool()

    runtime._check_idle_unload()

    assert fake.unloaded is False


def test_idle_unload_waits_for_queued_ai_work(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 1)
    runtime = _make_runtime(store, settings)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime._last_activity = time.monotonic() - 120

    class IdlePool:
        def activeThreadCount(self):
            return 0

    runtime._ai_pool = IdlePool()
    runtime._pending_ai_tasks.add(("text", 123))

    runtime._check_idle_unload()

    assert fake.unloaded is False


def test_text_embedding_touches_shared_idle_activity(qapp, store, settings):
    runtime = _make_runtime(store, settings)
    runtime._text_embedder = FakeEmbedder()
    old_activity = time.monotonic() - 120
    runtime._last_activity = old_activity

    runtime.embed_text("activity")

    assert runtime._last_activity > old_activity


def test_ocr_processing_touches_shared_idle_activity(qapp, store, settings):
    runtime = _make_runtime(store, settings, ocr=True, ocr_timing="immediate")
    runtime._ocr_engine = FakeOcrEngine()
    clip_id = store.add("image", {"image/png": PNG_1X1})
    old_activity = time.monotonic() - 120
    runtime._last_activity = old_activity

    runtime._process_clip_ocr(clip_id)

    assert _pump_until(qapp, lambda: clip_id not in store.clips_missing_ocr())
    assert runtime._last_activity > old_activity


def test_loading_ocr_engine_touches_shared_idle_activity(qapp, store, settings):
    runtime = _make_runtime(store, settings)
    runtime._ocr_engine = FakeOcrEngine()
    old_activity = time.monotonic() - 120
    runtime._last_activity = old_activity

    runtime.load_ocr_engine()

    assert runtime._last_activity > old_activity


def test_idle_unload_never_fires_when_minutes_is_zero(qapp, store, settings):
    settings.setValue("ai/model_idle_unload_minutes", 0)
    runtime = _make_runtime(store, settings)
    fake = FakeEmbedder()
    runtime._text_embedder = fake
    runtime._last_activity = time.monotonic() - 10_000

    runtime._check_idle_unload()

    assert fake.unloaded is False
