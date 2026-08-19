import os
import subprocess
import sys


def test_edit_dialog_find_bar_seeds_navigates_and_closes():
    script = r'''
from PySide6.QtWidgets import QApplication
from keeps.ui.expand_dialog import EditDialog

app = QApplication([])
dialog = EditDialog("before needle middle needle after", initial_query="needle")
assert not dialog._find_bar.isHidden()
assert dialog._editor.textCursor().selectedText() == "needle"
assert dialog._find_bar._counter.text() == "1 / 2"
dialog._find_bar.find_next()
assert dialog._find_bar._counter.text() == "2 / 2"
dialog._find_bar.find_next()
assert dialog._find_bar._counter.text() == "1 / 2"
dialog._find_bar.find_previous()
assert dialog._find_bar._counter.text() == "2 / 2"
dialog._find_bar.close()
assert dialog._find_bar.isHidden()
dialog.show_find()
assert not dialog._find_bar.isHidden()
'''
    environment = os.environ | {"QT_QPA_PLATFORM": "offscreen"}

    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr


def test_keyword_mode_does_not_start_semantic_query(tmp_path):
    script = r'''
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication
from keeps.ai.ranking import SearchMode
from keeps.store import Store
from keeps.ui.popup import ClipListModel

app = QApplication([])
store = Store(Path("store.db"))
store.add("text", {"text/plain": b"needle"})
calls = []
runtime = SimpleNamespace(
    rag_text_enabled=True,
    ocr_enabled=False,
    search_mode=SearchMode.KEYWORD,
    encode_query_async=lambda query, callback: calls.append(query),
)
model = ClipListModel(store, runtime)
model.set_query("needle")
assert calls == []
runtime.search_mode = SearchMode.SEMANTIC
model.set_query("needle")
assert calls == ["needle"]
store.close()
'''
    environment = os.environ | {
        "QT_QPA_PLATFORM": "offscreen",
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
