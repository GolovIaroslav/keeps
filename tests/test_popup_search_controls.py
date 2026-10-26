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
