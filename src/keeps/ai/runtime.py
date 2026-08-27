"""Qt-side AI glue: lazy model lifetime, idle-unload timer, async query search
(PLAN.md §9/§9.1). The only ai/* module allowed to import Qt -- models.py,
download.py, text_embed.py, ranking.py stay Qt-free and independently testable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from keeps import config
from keeps.ai import models
from keeps.ai.ranking import (
    DEFAULT_THRESHOLD,
    IMAGE_THRESHOLD,
    SearchMode,
    reciprocal_rank_fusion,
)
from keeps.store import Store

IDLE_CHECK_INTERVAL_MS = 30_000

# ai/ocr_timing=scheduled sweep cadence. Not a config key: PLAN.md §9.2
# explicitly allows hardcoding this rather than adding more settings surface.
SCHEDULED_SWEEP_INTERVAL_MS = 5 * 60 * 1000
OCR_TASK_PRIORITY = -1  # below default (0): background indexing, not user-facing
AI_TASK_MAX_THREADS = 1  # bound OCR/RAG working memory; model inference is CPU-heavy
SEMANTIC_REFRESH_BATCH = 8
logger = logging.getLogger(__name__)


def _release_unused_heap_memory() -> None:
    """Return freed native inference arenas to Linux after model unload.

    ONNX Runtime, OpenCV, and OpenBLAS allocate from several glibc arenas.
    Dropping their Python session objects frees those allocations, but glibc
    can retain the pages indefinitely in a long-lived daemon. ``malloc_trim``
    releases those already-free pages without touching live allocations.
    """
    try:
        import ctypes

        malloc_trim = ctypes.CDLL(None).malloc_trim
    except (AttributeError, OSError):
        return
    malloc_trim.argtypes = [ctypes.c_size_t]
    malloc_trim.restype = ctypes.c_int
    malloc_trim(0)


class _QuerySignals(QObject):
    # `object`, not `dict`: PySide6 marshals a `dict`-typed signal argument as
    # a C++ QVariantMap (string keys only) for the queued cross-thread
    # connection this needs (worker thread -> main thread) -- with our
    # int-keyed {clip_id: score} dict, that conversion silently fails and the
    # receiver gets an empty dict every time. `object` carries the raw Python
    # dict through untouched. Found live: RAG search always returned zero
    # semantic hits despite embeddings existing on disk.
    finished = Signal(str, object)  # (query, {clip_id: cosine_score})
    done = Signal()


class _EncodeQueryTask(QRunnable):
    """Runs off the main thread: embeds the query, scores it against every
    stored vector. sqlite/Qt objects must never be touched from here --
    `clip_ids_and_vecs` is plain (int, bytes) data fetched on the main thread
    beforehand.
    """

    def __init__(
        self, query: str, signals: _QuerySignals, encoders_and_vecs, is_current
    ) -> None:
        super().__init__()
        self._query = query
        self._signals = signals
        self._encoders_and_vecs = encoders_and_vecs
        self._is_current = is_current

    def run(self) -> None:
        try:
            if not self._is_current():
                return
            import numpy as np

            score_sets = []
            thresholds = []
            for encode_query, clip_ids_and_vecs, threshold in self._encoders_and_vecs:
                try:
                    query_vec = encode_query(self._query)
                except Exception:
                    logger.exception("semantic query encoder failed")
                    if not self._is_current():
                        return
                    continue
                if not self._is_current():
                    return
                scores = {}
                for clip_id, vec_bytes in clip_ids_and_vecs:
                    vec = np.frombuffer(vec_bytes, dtype=np.float32)
                    scores[clip_id] = float(np.dot(query_vec, vec))
                score_sets.append(scores)
                thresholds.append(threshold)
            if self._is_current():
                self._signals.finished.emit(
                    self._query,
                    reciprocal_rank_fusion(score_sets, thresholds=thresholds),
                )
        finally:
            self._signals.done.emit()


class _TextEmbedSignals(QObject):
    finished = Signal(int, str, object)  # (clip_id, source_hash, embedding_bytes | None)


class _ImageEmbedSignals(QObject):
    finished = Signal(int, str, object)  # (clip_id, source_hash, embedding_bytes | None)


class _ImageEmbedTask(QRunnable):
    def __init__(
        self, embed_fn, clip_id: int, source_hash: str, image_bytes: bytes, signals, is_current
    ):
        super().__init__()
        self._embed_fn = embed_fn
        self._clip_id = clip_id
        self._source_hash = source_hash
        self._image_bytes = image_bytes
        self._signals = signals
        self._is_current = is_current

    def run(self) -> None:
        if not self._is_current():
            self._signals.finished.emit(self._clip_id, self._source_hash, None)
            return
        try:
            vec_bytes = self._embed_fn(self._image_bytes)
        except Exception:
            logger.exception("visual embedding failed for clip %s", self._clip_id)
            self._signals.finished.emit(self._clip_id, self._source_hash, b"")
            return
        self._signals.finished.emit(
            self._clip_id,
            self._source_hash,
            vec_bytes if self._is_current() else None,
        )


class _TextEmbedTask(QRunnable):
    """Runs off the main thread: embeds a captured text/html clip's own
    content, so it can be found by RAG search later. Mirrors `_OcrTask`'s
    embed step, but for clips that never go through OCR.
    """

    def __init__(
        self,
        embed_fn,
        clip_id: int,
        source_hash: str,
        text: str,
        signals: _TextEmbedSignals,
        is_current,
    ) -> None:
        super().__init__()
        self._embed_fn = embed_fn
        self._clip_id = clip_id
        self._source_hash = source_hash
        self._text = text
        self._signals = signals
        self._is_current = is_current

    def run(self) -> None:
        if not self._is_current():
            self._signals.finished.emit(self._clip_id, self._source_hash, None)
            return
        try:
            vec_bytes = self._embed_fn(self._text)
        except Exception:
            logger.exception("text embedding failed for clip %s", self._clip_id)
            self._signals.finished.emit(self._clip_id, self._source_hash, b"")
            return
        self._signals.finished.emit(
            self._clip_id,
            self._source_hash,
            vec_bytes if self._is_current() else None,
        )


class _OcrSignals(QObject):
    finished = Signal(int, str, object, object)  # id, source_hash, ocr_text | None, vec


class _OcrTask(QRunnable):
    """Runs OCR off the main thread; embedding is decided on completion."""

    def __init__(
        self,
        ocr_engine,
        clip_id: int,
        source_hash: str,
        png_bytes: bytes,
        signals: _OcrSignals,
    ) -> None:
        super().__init__()
        self._ocr_engine = ocr_engine
        self._clip_id = clip_id
        self._source_hash = source_hash
        self._png_bytes = png_bytes
        self._signals = signals

    def run(self) -> None:
        try:
            text = self._ocr_engine.extract_text(self._png_bytes)
        except Exception:
            logger.exception("OCR failed for clip %s", self._clip_id)
            self._signals.finished.emit(self._clip_id, self._source_hash, None, None)
            return
        self._signals.finished.emit(self._clip_id, self._source_hash, text, None)


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
        code for code in codes if code in models.OCR_REC and is_downloaded_fn(models.OCR_REC[code])
    ]


class AiRuntime(QObject):
    """Owns the lazy TextEmbedder/OcrEngine and the search-mode toggle state.

    One instance lives for the daemon's lifetime (created in
    app.py::_run_daemon), shared by PopupWindow (search) and SettingsDialog
    (Model management).
    """

    semantic_index_changed = Signal()

    def __init__(self, store: Store, settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._settings = settings
        self._text_embedder = None
        self._text_embedder_lock = threading.Lock()
        self._image_embedder = None
        self._ocr_engine = None
        self._last_activity = 0.0
        self._semantic_generation = 0
        self._query_generation = 0
        self._query_task_active = False
        self._pending_query_request = None
        self._completed_semantic_tasks = {"text": 0, "image": 0}
        self._text_embed_backlog: deque[int] = deque()
        self._image_embed_backlog: deque[int] = deque()
        self._ocr_backlog: deque[int] = deque()
        # Keyword search is instant and predictable; semantic inference is
        # opt-in from the popup's mode selector.
        self._search_mode = SearchMode.KEYWORD

        # Serialized (maxThreadCount=1): query encoding and model indexing
        # share one pool so two ONNX sessions cannot multiply RSS. Queries
        # use the normal priority and jump ahead of background indexing;
        # background work stays below the default priority.
        self._ai_pool = QThreadPool(self)
        self._ai_pool.setMaxThreadCount(AI_TASK_MAX_THREADS)
        # Never put model indexing on Qt's process-wide pool. A backlog sweep
        # can contain hundreds of clips; the global pool would run many OCR
        # sessions concurrently and multiply ONNX Runtime's temporary arenas
        # into gigabytes of RSS.
        self._pending_ai_tasks: set[tuple[str, int]] = set()

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
    def search_mode(self) -> SearchMode:
        return self._search_mode

    @property
    def semantic_search_enabled(self) -> bool:
        return (
            (self.rag_text_enabled or self.image_semantic_enabled)
            and self.search_mode != SearchMode.KEYWORD
        )

    @property
    def image_semantic_enabled(self) -> bool:
        return bool(config.get(self._settings, "ai/image_semantic_enabled"))

    def set_search_mode(self, mode: SearchMode) -> None:
        if mode == self._search_mode:
            return
        self._search_mode = mode
        self._semantic_generation += 1
        self._query_generation += 1
        self._pending_query_request = None
        if self.semantic_search_enabled:
            self.run_text_embed_backlog_sweep()
            self.run_image_embed_backlog_sweep()
        else:
            self._text_embed_backlog.clear()
            self._image_embed_backlog.clear()

    def semantic_capabilities_changed(self) -> None:
        """Invalidate semantic work/results after a RAG or vision toggle."""
        self._semantic_generation += 1
        self._query_generation += 1
        self._pending_query_request = None
        if not self.rag_text_enabled:
            self._text_embed_backlog.clear()
        if not self.image_semantic_enabled:
            self._image_embed_backlog.clear()
        if not (self.rag_text_enabled or self.image_semantic_enabled):
            self.set_search_mode(SearchMode.KEYWORD)
        elif self.semantic_search_enabled:
            if self.rag_text_enabled:
                self.run_text_embed_backlog_sweep()
            if self.image_semantic_enabled:
                self.run_image_embed_backlog_sweep()
        self.semantic_index_changed.emit()

    @property
    def ai_task_max_threads(self) -> int:
        """Maximum number of background OCR/RAG inferences in flight."""
        return self._ai_pool.maxThreadCount()

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
        # Never drop a session while a query or background indexing task is
        # still using it. This matters with a large backlog: a single worker
        # can run for longer than the idle interval even though the model is
        # actively doing useful work.
        if self._ai_pool.activeThreadCount() or self._pending_ai_tasks:
            return
        idle = time.monotonic() - self._last_activity
        if idle < minutes * 60:
            return
        unloaded = False
        if self._text_embedder is not None and self._text_embedder.is_loaded:
            self._text_embedder.unload()
            unloaded = True
        if self._image_embedder is not None and self._image_embedder.is_loaded:
            self._image_embedder.unload()
            unloaded = True
        if self._ocr_engine is not None and self._ocr_engine.is_loaded:
            self.reset_ocr_engine()
            unloaded = True
        if unloaded:
            _release_unused_heap_memory()

    # -- text embedder lifecycle (Model management) -------------------------

    def _get_text_embedder(self):
        # Guards construction only: direct model-management calls can still
        # race with the serialized AI pool while lazily creating the embedder.
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

    def _get_image_embedder(self):
        if self._image_embedder is None:
            from keeps.ai.image_embed import ImageEmbedder

            files = models.IMAGE_EMBED.files
            self._image_embedder = ImageEmbedder(
                models.file_dest(models.IMAGE_EMBED, files[0]),
                models.file_dest(models.IMAGE_EMBED, files[1]),
                models.file_dest(models.IMAGE_EMBED, files[2]),
            )
        return self._image_embedder

    def image_embed_status(self) -> models.ModelStatus:
        loaded = self._image_embedder is not None and self._image_embedder.is_loaded
        return models.status(models.IMAGE_EMBED, loaded=loaded)

    def load_image_embedder(self) -> None:
        self._get_image_embedder().load()
        self._touch_activity()

    def unload_image_embedder(self) -> None:
        if self._image_embedder is not None:
            self._image_embedder.unload()

    # -- search ---------------------------------------------------------------

    def encode_query_async(self, query: str, on_done) -> None:
        """Score `query` against every stored embedding off the main thread.

        `on_done(query, scores)` fires back on the Qt event loop (queued
        connection, since the emitting task runs on a worker thread). The
        query text is echoed back so callers can discard stale results from
        a since-superseded search.
        """
        self._query_generation += 1
        query_generation = self._query_generation
        if not query.strip() or not self.semantic_search_enabled:
            self._pending_query_request = None
            on_done(query, {})
            return
        if self._query_task_active:
            # Keep only the latest lightweight request while one query owns
            # the serialized AI pool. In particular, do not duplicate every
            # embedding BLOB on each keystroke while an older query is still
            # queued/running.
            self._pending_query_request = (query, on_done, query_generation)
            return
        self._start_query(query, on_done, query_generation)

    def _start_query(self, query: str, on_done, query_generation: int) -> None:
        encoders_and_vecs = []
        if self.rag_text_enabled and (
            self._text_embedder is not None or models.is_downloaded(models.TEXT_EMBED)
        ):
            encoders_and_vecs.append(
                (
                    self._get_text_embedder().encode,
                    self._store.get_all_embeddings(models.TEXT_EMBED.name),
                    DEFAULT_THRESHOLD,
                )
            )
        if self.image_semantic_enabled and (
            self._image_embedder is not None or models.is_downloaded(models.IMAGE_EMBED)
        ):
            encoders_and_vecs.append(
                (
                    self._get_image_embedder().encode_text,
                    self._store.get_all_embeddings(models.IMAGE_EMBED.name),
                    IMAGE_THRESHOLD,
                )
            )
        if not encoders_and_vecs:
            on_done(query, {})
            return
        signals = _QuerySignals(self)
        signals.finished.connect(on_done)
        signals.done.connect(self._on_query_task_done)
        signals.done.connect(signals.deleteLater)
        generation = self._semantic_generation
        self._query_task_active = True
        self._ai_pool.start(
            _EncodeQueryTask(
                query,
                signals,
                encoders_and_vecs,
                lambda: (
                    self._semantic_generation == generation
                    and self._query_generation == query_generation
                    and self._search_mode != SearchMode.KEYWORD
                ),
            )
        )
        self._touch_activity()

    def _on_query_task_done(self) -> None:
        self._query_task_active = False
        pending = self._pending_query_request
        self._pending_query_request = None
        if pending is None or not self.semantic_search_enabled:
            return
        query, on_done, query_generation = pending
        if query_generation != self._query_generation:
            return
        self._start_query(query, on_done, query_generation)

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

    def embed_image(self, image_bytes: bytes) -> bytes:
        vec = self._get_image_embedder().encode_image(image_bytes)
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
            self._touch_activity()

    def unload_ocr_engine(self) -> None:
        if self._ocr_engine is not None:
            self._ocr_engine.unload()

    # -- OCR scheduling (PLAN.md §9.2) ---------------------------------------

    def on_clip_captured(self, clip_id: int, kind: str) -> None:
        """Connected to each capture watcher's `clip_added` signal."""
        if kind in ("text", "html") and self.semantic_search_enabled and self.rag_text_enabled:
            # No timing knob here (unlike OCR, §9.2): encode() is ~13ms warm
            # (PLAN.md §9 live smoke test), so always indexing immediately
            # needs no debounce/schedule setting of its own.
            self._enqueue_text_embed(clip_id, front=True)
        if kind == "image" and self.semantic_search_enabled and self.image_semantic_enabled:
            self._enqueue_image_embed(clip_id, front=True)
        if kind != "image" or not self.ocr_enabled:
            return
        if not self._store.clip_needs_ocr(clip_id):
            return
        timing = self.ocr_timing
        if timing == "immediate":
            self._enqueue_ocr(clip_id, front=True)
        elif timing == "delayed":
            if clip_id in self._pending_delayed_clip_ids:
                return
            self._pending_delayed_clip_ids.add(clip_id)
            self._delay_timer.start(int(self.ocr_delay_seconds * 1000))
        # "scheduled": nothing to do here -- the periodic sweep picks it up.

    @staticmethod
    def _queue_id(backlog: deque[int], clip_id: int, *, front: bool) -> None:
        if clip_id in backlog:
            return
        if front:
            backlog.appendleft(clip_id)
        else:
            backlog.append(clip_id)

    def _enqueue_text_embed(self, clip_id: int, *, front: bool = False) -> None:
        task_key = ("text", clip_id)
        if task_key in self._pending_ai_tasks or not self._store.clip_needs_embedding(
            clip_id, models.TEXT_EMBED.name
        ):
            return
        self._queue_id(self._text_embed_backlog, clip_id, front=front)
        self._drain_text_embed_backlog()

    def _process_clip_text_embed(self, clip_id: int) -> None:
        if not self._store.clip_needs_embedding(clip_id, models.TEXT_EMBED.name):
            return
        text = self._store.embedding_text(clip_id)
        if text is None:
            return
        if not text.strip():
            return
        source_hash = self._store.content_hash(clip_id)
        if source_hash is None:
            return
        signals = _TextEmbedSignals(self)
        signals.finished.connect(self._on_text_embed_done)
        generation = self._semantic_generation
        task = _TextEmbedTask(
            self.embed_text,
            clip_id,
            source_hash,
            text,
            signals,
            lambda: (
                self._semantic_generation == generation
                and self._search_mode != SearchMode.KEYWORD
            ),
        )
        self._pending_ai_tasks.add(("text", clip_id))
        self._ai_pool.start(task, OCR_TASK_PRIORITY)

    def _on_text_embed_done(
        self, clip_id: int, source_hash: str, vec_bytes: bytes | None
    ) -> None:
        self._pending_ai_tasks.discard(("text", clip_id))
        source_is_current = self._store.content_hash(clip_id) == source_hash
        if vec_bytes and source_is_current:
            self._store.set_embedding(clip_id, models.TEXT_EMBED.name, vec_bytes)
            self._completed_semantic_tasks["text"] += 1
            if (
                self._completed_semantic_tasks["text"] % SEMANTIC_REFRESH_BATCH == 0
                or (
                    not self._text_embed_backlog
                    and not any(kind == "text" for kind, _clip_id in self._pending_ai_tasks)
                )
            ):
                self.semantic_index_changed.emit()
        elif (
            (not source_is_current or vec_bytes is None)
            and self.semantic_search_enabled
            and self.rag_text_enabled
        ):
            self._enqueue_text_embed(clip_id, front=True)
        self._drain_text_embed_backlog()

    def _drain_text_embed_backlog(self) -> None:
        if not self.semantic_search_enabled or not self.rag_text_enabled:
            return
        if any(kind == "text" for kind, _clip_id in self._pending_ai_tasks):
            return
        while self._text_embed_backlog:
            clip_id = self._text_embed_backlog.popleft()
            self._process_clip_text_embed(clip_id)
            if ("text", clip_id) in self._pending_ai_tasks:
                return

    def _process_clip_image_embed(self, clip_id: int) -> None:
        if not self._store.clip_needs_image_embedding(clip_id, models.IMAGE_EMBED.name):
            return
        image_bytes = self._store.get_data(clip_id).get("image/png")
        if image_bytes is None:
            return
        source_hash = self._store.content_hash(clip_id)
        if source_hash is None:
            return
        signals = _ImageEmbedSignals(self)
        signals.finished.connect(self._on_image_embed_done)
        generation = self._semantic_generation
        task = _ImageEmbedTask(
            self.embed_image,
            clip_id,
            source_hash,
            image_bytes,
            signals,
            lambda: (
                self._semantic_generation == generation
                and self._search_mode != SearchMode.KEYWORD
            ),
        )
        self._pending_ai_tasks.add(("image", clip_id))
        self._ai_pool.start(task, OCR_TASK_PRIORITY)

    def _enqueue_image_embed(self, clip_id: int, *, front: bool = False) -> None:
        if (
            ("image", clip_id) in self._pending_ai_tasks
            or not self._store.clip_needs_image_embedding(
                clip_id, models.IMAGE_EMBED.name
            )
        ):
            return
        self._queue_id(self._image_embed_backlog, clip_id, front=front)
        self._drain_image_embed_backlog()

    def _on_image_embed_done(
        self, clip_id: int, source_hash: str, vec_bytes: bytes | None
    ) -> None:
        self._pending_ai_tasks.discard(("image", clip_id))
        source_is_current = self._store.content_hash(clip_id) == source_hash
        if vec_bytes and source_is_current:
            self._store.set_embedding(clip_id, models.IMAGE_EMBED.name, vec_bytes)
            self._completed_semantic_tasks["image"] += 1
            if (
                self._completed_semantic_tasks["image"] % SEMANTIC_REFRESH_BATCH == 0
                or (
                    not self._image_embed_backlog
                    and not any(
                        kind == "image" for kind, _clip_id in self._pending_ai_tasks
                    )
                )
            ):
                self.semantic_index_changed.emit()
        elif (
            (not source_is_current or vec_bytes is None)
            and self.semantic_search_enabled
            and self.image_semantic_enabled
        ):
            self._enqueue_image_embed(clip_id, front=True)
        self._drain_image_embed_backlog()

    def run_text_embed_backlog_sweep(self) -> None:
        """Picks up text/html and OCR clips still missing an embedding -- the
        one-time pass when semantic search is explicitly selected.
        """
        if not self.semantic_search_enabled or not self.rag_text_enabled:
            return
        pending_ids = {
            clip_id for kind, clip_id in self._pending_ai_tasks if kind == "text"
        }
        self._text_embed_backlog = deque(
            clip_id
            for clip_id in self._store.clips_missing_embedding(models.TEXT_EMBED.name)
            if clip_id not in pending_ids
        )
        self._completed_semantic_tasks["text"] = 0
        self._drain_text_embed_backlog()

    def run_image_embed_backlog_sweep(self) -> None:
        if not self.semantic_search_enabled or not self.image_semantic_enabled:
            return
        pending_ids = {
            clip_id for kind, clip_id in self._pending_ai_tasks if kind == "image"
        }
        self._image_embed_backlog = deque(
            clip_id
            for clip_id in self._store.clips_missing_image_embedding(models.IMAGE_EMBED.name)
            if clip_id not in pending_ids
        )
        self._completed_semantic_tasks["image"] = 0
        self._drain_image_embed_backlog()

    def _drain_image_embed_backlog(self) -> None:
        """Queue only bounded image bytes; large histories must not balloon RSS."""
        if not self.semantic_search_enabled or not self.image_semantic_enabled:
            return
        if any(kind == "image" for kind, _clip_id in self._pending_ai_tasks):
            return
        while self._image_embed_backlog:
            clip_id = self._image_embed_backlog.popleft()
            self._process_clip_image_embed(clip_id)
            if ("image", clip_id) in self._pending_ai_tasks:
                return

    def _flush_delayed_ocr(self) -> None:
        pending, self._pending_delayed_clip_ids = self._pending_delayed_clip_ids, set()
        for clip_id in pending:
            self._enqueue_ocr(clip_id)

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
        pending_ids = {
            clip_id for kind, clip_id in self._pending_ai_tasks if kind == "ocr"
        }
        self._ocr_backlog = deque(
            clip_id
            for clip_id in self._store.clips_missing_ocr()
            if clip_id not in pending_ids
        )
        self._drain_ocr_backlog()

    def _enqueue_ocr(self, clip_id: int, *, front: bool = False) -> None:
        if ("ocr", clip_id) in self._pending_ai_tasks or not self._store.clip_needs_ocr(
            clip_id
        ):
            return
        self._queue_id(self._ocr_backlog, clip_id, front=front)
        self._drain_ocr_backlog()

    def _process_clip_ocr(self, clip_id: int) -> None:
        if not self._store.clip_needs_ocr(clip_id):
            return
        mime_data = self._store.get_data(clip_id)
        png_bytes = mime_data.get("image/png")
        if png_bytes is None:
            return
        source_hash = self._store.content_hash(clip_id)
        if source_hash is None:
            return
        engine = self._get_ocr_engine()
        if engine is None:
            return
        signals = _OcrSignals(self)
        signals.finished.connect(self._on_ocr_done)
        task = _OcrTask(engine, clip_id, source_hash, png_bytes, signals)
        self._pending_ai_tasks.add(("ocr", clip_id))
        self._ai_pool.start(task, OCR_TASK_PRIORITY)

    def _on_ocr_done(
        self,
        clip_id: int,
        source_hash: str,
        text: str | None,
        vec_bytes: bytes | None,
    ) -> None:
        self._pending_ai_tasks.discard(("ocr", clip_id))
        self._touch_activity()
        if self._store.content_hash(clip_id) != source_hash:
            if self.ocr_enabled:
                self._enqueue_ocr(clip_id, front=True)
            self._drain_ocr_backlog()
            return
        if text is None:
            self._drain_ocr_backlog()
            return
        self._store.set_ocr_text(clip_id, text)
        if vec_bytes is not None:
            self._store.set_embedding(clip_id, models.TEXT_EMBED.name, vec_bytes)
        elif self.semantic_search_enabled and self.rag_text_enabled and text.strip():
            self._enqueue_text_embed(clip_id, front=True)
        self._drain_ocr_backlog()

    def _drain_ocr_backlog(self) -> None:
        if not self.ocr_enabled:
            return
        if any(kind == "ocr" for kind, _clip_id in self._pending_ai_tasks):
            return
        while self._ocr_backlog:
            clip_id = self._ocr_backlog.popleft()
            self._process_clip_ocr(clip_id)
            if ("ocr", clip_id) in self._pending_ai_tasks:
                return
