"""Runtime integration for the single shared EmbeddingGemma backend."""

import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QSettings

from keeps.ai import models
from keeps.ai.ranking import SearchMode, reciprocal_rank_fusion
from keeps.ai.runtime import AiRuntime
from keeps.store import Store


class FakeUnifiedEncoder:
    is_loaded = True

    def __init__(self):
        self.calls = []

    def load(self):
        self.is_loaded = True

    def unload(self):
        self.is_loaded = False

    def encode_text(self, text):
        self.calls.append(("text", text))
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)

    def encode_image(self, data):
        self.calls.append(("image", len(data)))
        return np.array([0.99, 0.1, 0.0], dtype=np.float32)

    def encode_query(self, query):
        self.calls.append(("query", query))
        return np.array([1.0, 0.0, 0.0], dtype=np.float32)


@pytest.fixture
def runtime(tmp_path):
    app = QCoreApplication.instance() or QCoreApplication([])
    store = Store(tmp_path / "keeps.db", max_items=20)
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    settings.setValue("ai/embedding_backend", "unified")
    settings.setValue("ai/auto_index_enabled", True)
    settings.setValue("ai/rag_text_enabled", True)
    settings.setValue("ai/image_semantic_enabled", True)
    settings.setValue("ai/ocr_enabled", False)
    engine = AiRuntime(store, settings)
    encoder = FakeUnifiedEncoder()
    engine._unified_embedder = encoder
    yield app, store, engine, encoder
    engine._ai_pool.waitForDone(5000)
    store.close()


def _until(app, predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(.01)
    return False


@pytest.mark.parametrize(
    ("requested", "downloaded", "expected"),
    [
        ("auto", False, "legacy"),
        ("auto", True, "unified"),
        ("legacy", True, "legacy"),
        ("unified", False, "unified"),
    ],
)
def test_backend_selected_only_at_startup(requested, downloaded, expected):
    assert models.selected_embedding_backend(requested, downloaded) == expected


def test_unified_backend_never_mixes_legacy_vector_spaces(runtime):
    app, store, ai, fake = runtime
    text = store.add("text", {"text/plain": b"horse and dog"})
    image = store.add("image", {"image/png": b"image fixture"})
    assert ai.embedding_backend == "unified"
    assert ai.text_model_spec == ai.image_model_spec == models.UNIFIED_EMBED
    assert ai.text_embedding_key != ai.image_embedding_key
    ai.on_clip_captured(text, "text")
    ai.on_clip_captured(image, "image")
    assert _until(
        app,
        lambda: bool(store.get_all_embeddings(models.UNIFIED_TEXT_KEY))
        and bool(store.get_all_embeddings(models.UNIFIED_IMAGE_KEY)),
    )
    assert not store.get_all_embeddings(models.TEXT_EMBED.name)
    assert not store.get_all_embeddings(models.IMAGE_EMBED.name)
    assert {kind for kind, _ in fake.calls} >= {"text", "image"}

    ai.set_search_mode(SearchMode.SEMANTIC)
    result = []
    ai.encode_query_async("pet animal", lambda q, scores: result.append((q, scores)))
    assert _until(app, lambda: bool(result))
    assert result[0][0] == "pet animal"
    assert {text, image} <= result[0][1].keys()
    assert result[0][1].origins[text] == frozenset({"text"})
    assert result[0][1].origins[image] == frozenset({"image"})


def test_unified_rank_gate_filters_relative_noise():
    scores = {1: .79, 2: .77, 3: .68, 4: .64}
    kept = reciprocal_rank_fusion(
        [scores], thresholds=[.62], relative_margin=.045,
    )
    assert set(kept) == {1, 2}
    assert reciprocal_rank_fusion([scores], thresholds=[.62]) == {
        1: pytest.approx(1.0),
        2: pytest.approx(61 / 62),
        3: pytest.approx(61 / 63),
        4: pytest.approx(61 / 64),
    }
