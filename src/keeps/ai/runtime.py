"""Qt-side AI glue: lazy model lifetime, idle-unload timer, async query search
(PLAN.md §9/§9.1). The only ai/* module allowed to import Qt -- models.py,
download.py, text_embed.py, ranking.py stay Qt-free and independently testable.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from keeps import config
from keeps.ai import models
from keeps.ai.ranking import SearchMode
from keeps.store import Store

IDLE_CHECK_INTERVAL_MS = 30_000

# ai/ocr_timing=scheduled sweep cadence. Not a config key: PLAN.md §9.2
# explicitly allows hardcoding this rather than adding more settings surface.
SCHEDULED_SWEEP_INTERVAL_MS = 5 * 60 * 1000
OCR_TASK_PRIORITY = -1  # below default (0): background indexing, not user-facing


class _QuerySignals(QObject):
    # `object`, not `dict`: PySide6 marshals a `dict`-typed signal argument as
    # a C++ QVariantMap (string keys only) for the queued cross-thread
    # connection this needs (worker thread -> main thread) -- with our
    # int-keyed {clip_id: score} dict, that conversion silently fails and the
    # receiver gets an empty dict every time. `object` carries the raw Python
    # dict through untouched. Found live: RAG search always returned zero
    # semantic hits despite embeddings existing on disk.
    finished = Signal(str, object)  # (query, {clip_id: cosine_score})


class _EncodeQueryTask(QRunnable):
    """Runs off the main thread: embeds the query, scores it against every
    stored vector. sqlite/Qt objects must never be touched from here --
    `clip_ids_and_vecs` is plain (int, bytes) data fetched on the main thread
    beforehand.
    """

    def __init__(self, embedder, query: str, signals: _QuerySignals, clip_ids_and_vecs) -> None:
        super().__init__()
        self._embedder = embedder
        self._query = query
        self._signals = signals
        self._clip_ids_and_vecs = clip_ids_and_vecs

    def run(self) -> None:
        import numpy as np

        query_vec = self._embedder.encode(self._query)
        scores = {}
        for clip_id, vec_bytes in self._clip_ids_and_vecs:
            vec = np.frombuffer(vec_bytes, dtype=np.float32)
            scores[clip_id] = float(np.dot(query_vec, vec))
        self._signals.finished.emit(self._query, scores)


class _TextEmbedSignals(QObject):
    finished = Signal(int, bytes)  # (clip_id, embedding_bytes)


class _TextEmbedTask(QRunnable):
    """Runs off the main thread: embeds a captured text/html clip's own
    content, so it can be found by RAG search later. Mirrors `_OcrTask`'s
    embed step, but for clips that never go through OCR.
    """

    def __init__(self, embed_fn, clip_id: int, text: str, signals: _TextEmbedSignals) -> None:
        super().__init__()
        self._embed_fn = embed_fn
        self._clip_id = clip_id
        self._text = text
        self._signals = signals

    def run(self) -> None:
        vec_bytes = self._embed_fn(self._text)
        self._signals.finished.emit(self._clip_id, vec_bytes)


class _OcrSignals(QObject):
    finished = Signal(int, str, object)  # (clip_id, ocr_text, embedding_bytes | None)


class _OcrTask(QRunnable):
    """Runs off the main thread: OCR the image, and (if RAG is on) embed the
    recognized text. `embed_fn` must be a pure/thread-safe callable (see
    AiRuntime.embed_text) -- no Store/Qt access from here.
    """

    def __init__(
        self, ocr_engine, clip_id: int, png_bytes: bytes, signals: _OcrSignals, embed_fn
    ) -> None:
        super().__init__()
        self._ocr_engine = ocr_engine
        self._clip_id = clip_id
        self._png_bytes = png_bytes
        self._signals = signals
        self._embed_fn = embed_fn

    def run(self) -> None:
        text = self._ocr_engine.extract_text(self._png_bytes)
        vec_bytes = self._embed_fn(text) if (self._embed_fn is not None and text.strip()) else None
        self._signals.finished.emit(self._clip_id, text, vec_bytes)


def available_ocr_language_codes(
    codes: list[str],
    is_downloaded_fn: Callable[[models.ModelSpec], bool] = models.is_downloaded,
) -> list[str]:
    """Filter requested language codes (ai/ocr_languages, already parsed) down
    to the ones that are both known (a key of models.OCR_REC) and currently
    downloaded -- and only if the shared detector is downloaded too, since no
    recognizer can run without it. Order is preserved from `codes`.

    Pure logic, independent of AiRuntime/Qt, so it's directly unit-testable
    without constructing a QCoreApplication.
    """
    if not is_downloaded_fn(models.OCR_DET):
        return []
    return [
        code
        for code in codes
        if code in models.OCR_REC and is_downloaded_fn(models.OCR_REC[code])
    ]


class AiRuntime(QObject):
    """Owns the lazy TextEmbedder/OcrEngine and the search-mode toggle state.

    One instance lives for the daemon's lifetime (created in
    app.py::_run_daemon), shared by PopupWindow (search) and SettingsDialog
    (Model management).
    """

    def __init__(self, store: Store, settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._settings = settings
        self._text_embedder = None
        self._text_embedder_lock = threading.Lock()
        self._ocr_engine = None
        self._last_activity = 0.0
        self.search_mode = SearchMode.BLENDED

        # Serialized (maxThreadCount=1): TextEmbedder.load() is not safe to
        # race from two threads at once, and a single short query encode is
        # fast enough that serializing has no visible cost.
        self._query_pool = QThreadPool(self)
        self._query_pool.setMaxThreadCount(1)

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(IDLE_CHECK_INTERVAL_MS)
        self._idle_timer.timeout.connect(self._check_idle_unload)
        self._idle_timer.start()

        # ai/ocr_timing=delayed: debounced from the *last* capture, not a
        # fixed delay per clip (PLAN.md §9.2) -- each new image restarts the
        # timer, and the whole pending batch is processed once it fires.
        self._pending_delayed_clip_ids: set[int] = set()
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.timeout.connect(self._flush_delayed_ocr)

        # ai/ocr_timing=scheduled: always ticking: the slot itself checks
        # whether that mode is currently selected, so toggling settings at
        # runtime doesn't require starting/stopping this timer reactively.
        self._scheduled_timer = QTimer(self)
        self._scheduled_timer.setInterval(SCHEDULED_SWEEP_INTERVAL_MS)
        self._scheduled_timer.timeout.connect(self._on_scheduled_tick)
        self._scheduled_timer.start()

    @property
    def rag_text_enabled(self) -> bool:
        return bool(config.get(self._settings, "ai/rag_text_enabled"))

    @property
    def ocr_enabled(self) -> bool:
        return bool(config.get(self._settings, "ai/ocr_enabled"))

    @property
    def ocr_timing(self) -> str:
        return str(config.get(self._settings, "ai/ocr_timing"))

    @property
    def ocr_delay_seconds(self) -> float:
        return float(config.get(self._settings, "ai/ocr_delay_seconds"))

    def _touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def _check_idle_unload(self) -> None:
        minutes = float(config.get(self._settings, "ai/model_idle_unload_minutes"))
        if minutes <= 0:
            return  # 0 = never auto-unload
        if self._text_embedder is None or not self._text_embedder.is_loaded:
            return
        if time.monotonic() - self._last_activity >= minutes * 60:
            self._text_embedder.unload()

    # -- text embedder lifecycle (Model management) -------------------------

    def _get_text_embedder(self):
        # Guards construction only: query encoding (_query_pool, maxThreadCount=1)
        # and OCR indexing (QThreadPool.globalInstance()) are separate pools
        # that could both race to lazily create the embedder on first use.
        with self._text_embedder_lock:
            if self._text_embedder is None:
                from keeps.ai.text_embed import TextEmbedder

                weights = models.file_dest(models.TEXT_EMBED, models.TEXT_EMBED.files[0])
                tokenizer = models.file_dest(models.TEXT_EMBED, models.TEXT_EMBED.files[1])
                self._text_embedder = TextEmbedder(weights, tokenizer)
            return self._text_embedder

    def text_embed_status(self) -> models.ModelStatus:
        loaded = self._text_embedder is not None and self._text_embedder.is_loaded
        return models.status(models.TEXT_EMBED, loaded=loaded)

    def load_text_embedder(self) -> None:
        self._get_text_embedder().load()
        self._touch_activity()

    def unload_text_embedder(self) -> None:
        if self._text_embedder is not None:
            self._text_embedder.unload()

    # -- search ---------------------------------------------------------------

    def encode_query_async(self, query: str, on_done) -> None:
        """Score `query` against every stored embedding off the main thread.

        `on_done(query, scores)` fires back on the Qt event loop (queued
        connection, since the emitting task runs on a worker thread). The
        query text is echoed back so callers can discard stale results from
        a since-superseded search.
        """
        if not query.strip():
            on_done(query, {})
            return
        embedder = self._get_text_embedder()
        clip_ids_and_vecs = self._store.get_all_embeddings(models.TEXT_EMBED.name)
        signals = _QuerySignals(self)
        signals.finished.connect(on_done)
        self._query_pool.start(_EncodeQueryTask(embedder, query, signals, clip_ids_and_vecs))
        self._touch_activity()

    def embed_text(self, text: str) -> bytes:
        """Compute an embedding as float32 bytes, ready for `store.set_embedding`.

        Pure computation, safe to call from a worker thread (unlike Store,
        which is bound to the thread that opened the sqlite connection --
        callers must persist the result back on the main thread). Used by
        the OCR pipeline to embed newly-recognized text.
        """
        vec = self._get_text_embedder().encode(text)
        self._touch_activity()
        return vec.astype("float32").tobytes()

    # -- OCR lifecycle (Model management) ------------------------------------

    def _get_ocr_engine(self):
        """Build (and cache) the OcrEngine from the user's current language
        selection (ai/ocr_languages) and what's actually downloaded on disk.

        Returns None if no selected language is usable yet (nothing
        downloaded, or the user unchecked everything) -- OcrEngine itself
        refuses to construct with zero recognizers. Callers must handle a
        None return instead of assuming an engine is always available.
        """
        if self._ocr_engine is None:
            codes = config.parse_ocr_languages(config.get(self._settings, "ai/ocr_languages"))
            available = available_ocr_language_codes(codes)
            if not available:
                return None

            from keeps.ai.ocr import OcrEngine, RecognizerConfig, dict_path_for

            det = models.file_dest(models.OCR_DET, models.OCR_DET.files[0])
            recognizers = [
                RecognizerConfig(
                    code,
                    models.file_dest(models.OCR_REC[code], models.OCR_REC[code].files[0]),
                    dict_path_for(code),
                )
                for code in available
            ]
            self._ocr_engine = OcrEngine(det, recognizers)
        return self._ocr_engine

    def reset_ocr_engine(self) -> None:
        """Drop the cached OcrEngine so the next _get_ocr_engine() call
        rebuilds it from the current config selection + on-disk state.

        Called by Settings > AI whenever the language selection changes or a
        download finishes, so newly enabled/downloaded languages take effect
        immediately, no daemon restart needed (matching every other setting
        in this app).
        """
        if self._ocr_engine is not None:
            self._ocr_engine.unload()
        self._ocr_engine = None

    def ocr_status(self) -> models.ModelStatus:
        loaded = self._ocr_engine is not None and self._ocr_engine.is_loaded
        codes = config.parse_ocr_languages(config.get(self._settings, "ai/ocr_languages"))
        if not available_ocr_language_codes(codes):
            return models.ModelStatus.NOT_DOWNLOADED
        return models.ModelStatus.LOADED if loaded else models.ModelStatus.DOWNLOADED

    def load_ocr_engine(self) -> None:
        engine = self._get_ocr_engine()
        if engine is not None:
            engine.load()

    def unload_ocr_engine(self) -> None:
        if self._ocr_engine is not None:
            self._ocr_engine.unload()

    # -- OCR scheduling (PLAN.md §9.2) ---------------------------------------

    def on_clip_captured(self, clip_id: int, kind: str) -> None:
        """Connected to each capture watcher's `clip_added` signal."""
        if kind in ("text", "html") and self.rag_text_enabled:
            # No timing knob here (unlike OCR, §9.2): encode() is ~13ms warm
            # (PLAN.md §9 live smoke test), so always indexing immediately
            # needs no debounce/schedule setting of its own.
            self._process_clip_text_embed(clip_id)
        if kind != "image" or not self.ocr_enabled:
            return
        timing = self.ocr_timing
        if timing == "immediate":
            self._process_clip_ocr(clip_id)
        elif timing == "delayed":
            self._pending_delayed_clip_ids.add(clip_id)
            self._delay_timer.start(int(self.ocr_delay_seconds * 1000))
        # "scheduled": nothing to do here -- the periodic sweep picks it up.

    def _process_clip_text_embed(self, clip_id: int) -> None:
        mime_data = self._store.get_data(clip_id)
        text = mime_data.get("text/plain", b"").decode("utf-8", errors="replace")
        if not text.strip():
            return
        signals = _TextEmbedSignals(self)
        signals.finished.connect(self._on_text_embed_done)
        task = _TextEmbedTask(self.embed_text, clip_id, text, signals)
        QThreadPool.globalInstance().start(task, OCR_TASK_PRIORITY)

    def _on_text_embed_done(self, clip_id: int, vec_bytes: bytes) -> None:
        self._store.set_embedding(clip_id, models.TEXT_EMBED.name, vec_bytes)

    def run_text_embed_backlog_sweep(self) -> None:
        """Picks up every text/html clip still missing an embedding -- the
        one-time pass over pre-existing history on first enabling RAG.
        """
        if not self.rag_text_enabled:
            return
        for clip_id in self._store.clips_missing_embedding(models.TEXT_EMBED.name):
            self._process_clip_text_embed(clip_id)

    def _flush_delayed_ocr(self) -> None:
        pending, self._pending_delayed_clip_ids = self._pending_delayed_clip_ids, set()
        for clip_id in pending:
            self._process_clip_ocr(clip_id)

    def _on_scheduled_tick(self) -> None:
        if self.ocr_enabled and self.ocr_timing == "scheduled":
            self.run_ocr_backlog_sweep()

    def run_ocr_backlog_sweep(self) -> None:
        """Picks up every image clip still missing ocr_text -- the one-time
        pass over pre-existing history on first enabling OCR, and the engine
        behind ai/ocr_timing=scheduled (PLAN.md §9.2).
        """
        if not self.ocr_enabled:
            return
        for clip_id in self._store.clips_missing_ocr():
            self._process_clip_ocr(clip_id)

    def _process_clip_ocr(self, clip_id: int) -> None:
        mime_data = self._store.get_data(clip_id)
        png_bytes = mime_data.get("image/png")
        if png_bytes is None:
            return
        engine = self._get_ocr_engine()
        if engine is None:
            return
        embed_fn = self.embed_text if self.rag_text_enabled else None
        signals = _OcrSignals(self)
        signals.finished.connect(self._on_ocr_done)
        task = _OcrTask(engine, clip_id, png_bytes, signals, embed_fn)
        QThreadPool.globalInstance().start(task, OCR_TASK_PRIORITY)

    def _on_ocr_done(self, clip_id: int, text: str, vec_bytes: bytes | None) -> None:
        self._store.set_ocr_text(clip_id, text)
        if vec_bytes is not None:
            self._store.set_embedding(clip_id, models.TEXT_EMBED.name, vec_bytes)
