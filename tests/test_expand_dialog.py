import os
import subprocess
import sys

from keeps.ui.expand_dialog import _match_spans, _utf16_offset


def test_find_spans_use_the_same_unicode_normalization_as_popup_search():
    assert _match_spans("Straße and STRASSE", "strasse") == [(0, 6), (11, 18)]
    assert _match_spans("Straße", "s") == [(0, 1), (4, 5)]


def test_qt_cursor_offset_counts_astral_characters_as_utf16():
    assert _utf16_offset("😀 needle", 2) == 3


def test_edit_dialog_find_bar_seeds_navigates_and_closes():
    script = r'''
from PySide6.QtWidgets import QApplication
from keeps.ui.expand_dialog import EditDialog

app = QApplication([])
dialog = EditDialog("😀 before needle middle needle after", initial_query="needle")
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
