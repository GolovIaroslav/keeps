import os
import subprocess
import sys


def test_popup_search_sort_controls_persist_and_filter_files(tmp_path):
    script = r'''
from pathlib import Path

from PySide6.QtWidgets import QApplication

from keeps import config
from keeps.store import Store
from keeps.ui.popup import PopupWindow

app = QApplication([])
store = Store(Path("store.db"), max_items=20)
short = store.add("text", {"text/plain": b"/projects"})
long = store.add("text", {"text/plain": b"projects " + b"filler " * 100})
files = store.add("files", {"text/uri-list": b"file:///tmp/projects.txt"})

popup = PopupWindow(store)
popup.model.set_query("projects")
def ids():
    return [popup.model.clip_at(i).id for i in range(popup.model.rowCount())]

assert popup._sort_button.text() == "Newest"
assert ids() == [files, long, short]
assert popup._content_filter_combo.findData("files") >= 0

popup._sort_menu.actions()[1].trigger()
assert popup._sort_button.text() == "Best match"
assert ids()[0] == short

popup._content_filter_combo.setCurrentIndex(
    popup._content_filter_combo.findData("files")
)
assert ids() == [files]

popup._content_filter_combo.setCurrentIndex(
    popup._content_filter_combo.findData("all")
)
popup._sort_menu.actions()[2].trigger()
assert popup._sort_button.text() == "Oldest"
assert ids() == [short, long, files]
assert config.get(config.open_settings(), "popup/search_sort_order") == "oldest"

popup.close()
reopened = PopupWindow(store)
assert reopened._sort_button.text() == "Oldest"
reopened.model.set_query("projects")
assert [reopened.model.clip_at(i).id for i in range(reopened.model.rowCount())] == [
    short, long, files
]
reopened.close()
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_image_ocr_and_semantic_results_obey_content_filter(tmp_path):
    script = r'''
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtWidgets import QApplication
from keeps.ai.ranking import SearchMode
from keeps.store import Store
from keeps.ui.popup import ClipListModel

app = QApplication([])
store = Store(Path("store.db"), max_items=20)
image = store.add("image", {"image/png": b"not-a-real-png"})
text = store.add("text", {"text/plain": b"blueprint"})
file = store.add("files", {"text/uri-list": b"file:///tmp/blueprint.pdf"})
store.set_ocr_text(image, "blueprint")
runtime = SimpleNamespace(
    rag_text_enabled=True,
    image_semantic_enabled=True,
    ocr_enabled=True,
    search_mode=SearchMode.SEMANTIC,
    encode_query_async=lambda query, cb: cb(query, {
        text: 0.9, image: 0.8, file: 0.7
    }),
)
model = ClipListModel(store, runtime)
model.set_query("blueprint")
assert model.rowCount() == 3
model.set_content_filter("images")
assert [model.clip_at(i).id for i in range(model.rowCount())] == [image]
runtime.search_mode = SearchMode.KEYWORD
model.set_query("blueprint")
assert [model.clip_at(i).id for i in range(model.rowCount())] == [image]
model.set_content_filter("files")
assert [model.clip_at(i).id for i in range(model.rowCount())] == [file]
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_popup_auto_index_controls_are_accessible_and_report_old_pause(tmp_path):
    script = r'''
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from keeps import config
from keeps.ai.ranking import SearchMode
from keeps.store import Store
from keeps.ui.popup import PopupWindow

class FakeIndexRuntime:
    rag_text_enabled = True
    ocr_enabled = True
    image_semantic_enabled = True
    search_mode = SearchMode.KEYWORD
    manual_index_active = False
    auto_index_enabled = False

    def __init__(self):
        self.calls = []

    def missing_index_counts(self):
        return {"text": 5, "images": 2, "ocr": 3}

    def indexable_missing_counts(self):
        return self.missing_index_counts()

    def set_auto_index_enabled(self, enabled):
        self.calls.append(("toggle", enabled))
        self.auto_index_enabled = enabled

    def index_missing_now(self):
        self.calls.append(("manual",))
        return 10

app = QApplication([])
settings = config.open_settings()
settings.setValue("ai/auto_index_paused_since", int(time.time() - 4 * 86400))
store = Store(Path("store.db"))
runtime = FakeIndexRuntime()
popup = PopupWindow(store, runtime)
popup.resize(340, 480)
popup.show()
popup.refresh()
app.processEvents()

assert popup._auto_index_box.text() == "Auto-index"
assert popup._auto_index_box.isChecked() is False
assert popup._auto_index_box.isVisible()
assert popup._index_missing_button.accessibleName() == "Index missing"
assert popup._index_missing_button.isEnabled()
assert "10" in popup._index_status_label.text()
assert "Paused" in popup._index_status_label.toolTip()
assert popup.search_edit.width() >= 75

popup._index_missing_button.click()
assert ("manual",) in runtime.calls
popup._auto_index_box.click()
assert ("toggle", True) in runtime.calls
assert popup._auto_index_box.isChecked() is True
runtime.search_mode = SearchMode.BLENDED
popup._update_mode_combo()
assert not popup._sort_button.isEnabled()
assert not popup._sort_button.isVisible()
runtime.search_mode = SearchMode.SEMANTIC
popup._update_mode_combo()
assert not popup._sort_button.isEnabled()
assert not popup._sort_button.isVisible()
runtime.search_mode = SearchMode.KEYWORD
popup._update_mode_combo()
assert popup._sort_button.isEnabled()
assert popup._sort_button.isVisible()
runtime.indexable_missing_counts = lambda: {"text": 0, "images": 0, "ocr": 0}
popup._refresh_index_status()
assert not popup._index_missing_button.isEnabled()
assert "Download" in popup._index_missing_button.toolTip()
popup.close()
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ
        | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

def test_scoped_semantic_search_admits_hits_outside_global_top_twenty(tmp_path):
    script = r'''
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from keeps.ai.ranking import SearchMode
from keeps.store import Store
from keeps.ui.popup import ClipListModel

app = QApplication([])
store = Store(Path("store.db"))
group = store.create_group("Saved")
target = store.add("text", {"text/plain": b"unrelated recording"})
store.set_group_many([target], group)
store.set_pinned(target, True)
others = [
    store.add("text", {"text/plain": f"record {i}".encode()})
    for i in range(30)
]
scores = {clip_id: 0.99 - index * 0.01 for index, clip_id in enumerate(others)}
scores[target] = 0.51
runtime = SimpleNamespace(
    rag_text_enabled=True,
    image_semantic_enabled=False,
    ocr_enabled=False,
    search_mode=SearchMode.SEMANTIC,
    encode_query_async=lambda query, cb: cb(query, scores),
)
model = ClipListModel(store, runtime)
for mode in (SearchMode.SEMANTIC, SearchMode.BLENDED):
    runtime.search_mode = mode
    for scope in (f"group:{group}", "pinned"):
        model.set_scope(scope)
        model.set_query("missing query")
        assert [model.clip_at(i).id for i in range(model.rowCount())] == [target], (
            mode, scope
        )
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

def test_meaning_does_not_highlight_incidental_query_tokens(tmp_path):
    script = r'''
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from keeps.ai.ranking import SearchMode
from keeps.store import Store
from keeps.ui.popup import ClipListModel

app = QApplication([])
store = Store(Path("store.db"))
clip = store.add("text", {"text/plain": b"an api key lives here"})
runtime = SimpleNamespace(
    rag_text_enabled=True,
    image_semantic_enabled=False,
    ocr_enabled=False,
    search_mode=SearchMode.SEMANTIC,
    encode_query_async=lambda q, done: done(q, {clip: 0.91}),
)
model = ClipListModel(store, runtime)
model.set_query("api key")
assert model.rowCount() == 1
assert model.match_reason(clip).value == "semantic"
assert model.highlight_query(clip) == ""

runtime.search_mode = SearchMode.KEYWORD
model.set_query("api key")
assert model.highlight_query(clip) == "api key"
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr

def test_semantic_provenance_shows_visual_and_ocr_source(tmp_path):
    script = r'''
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from keeps.ai.ranking import SearchMode, SemanticScores
from keeps.store import Store
from keeps.ui.popup import ClipListModel

app = QApplication([])
store = Store(Path("store.db"), max_items=20)
text = store.add("text", {"text/plain": b"pet animal"})
image = store.add("image", {"image/png": b"test image"})
store.set_ocr_text(image, "cute kitten")
found = SemanticScores(
    {text: .98, image: .91},
    {text: frozenset({"text"}), image: frozenset({"image", "text"})},
)
runtime = SimpleNamespace(
    rag_text_enabled=True,
    image_semantic_enabled=True,
    ocr_enabled=True,
    search_mode=SearchMode.SEMANTIC,
    encode_query_async=lambda query, done: done(query, found),
)
model = ClipListModel(store, runtime)
model.set_query("pet animal")
assert model.rowCount() == 2
assert model.match_source_label(text) == "meaning"
assert model.match_source_label(image) == "visual+OCR"
assert model.highlight_query(text) == ""
assert model.highlight_query(image) == ""
descriptions = [
    model.data(model.index(row, 0), Qt.ItemDataRole.ToolTipRole)
    for row in range(model.rowCount())
]
assert any("visual similarity and OCR text meaning" in text for text in descriptions)
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
