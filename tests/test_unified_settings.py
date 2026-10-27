"""GUI regression runs in an isolated Qt application process.

Other tests intentionally create QCoreApplication; Qt cannot upgrade that
singleton into QApplication in the same process.
"""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("active", ["legacy", "unified"])
def test_inactive_embedding_engine_cannot_be_loaded_into_ram(tmp_path, active):
    script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QGroupBox, QPushButton

from keeps import config
from keeps.ai import models
from keeps.ai.runtime import AiRuntime
from keeps.store import Store
from keeps.ui.settings import SettingsDialog

app = QApplication([])
active = sys.argv[1]
tmp = Path(sys.argv[2])
settings = QSettings(str(tmp / "settings.ini"), QSettings.Format.IniFormat)
settings.setValue("ai/embedding_backend", active)
settings.setValue("ai/auto_index_enabled", False)
config.open_settings = lambda: settings
models.status = lambda spec, loaded, downloading=False: models.ModelStatus.DOWNLOADED
store = Store(tmp / "store.db", max_items=20)
runtime = AiRuntime(store, settings)
dialog = SettingsDialog(ai_runtime=runtime, store=store)
dialog.show()
app.processEvents()
groups = {
    box.title(): box
    for box in dialog.findChildren(QGroupBox)
    if "embeddings" in box.title() or "EmbeddingGemma" in box.title()
}
assert len(groups) == 3, groups

def enabled(title):
    load = [
        b for b in groups[title].findChildren(QPushButton)
        if b.text() == "Load into RAM"
    ]
    assert len(load) == 1
    return load[0].isEnabled()

assert enabled("EmbeddingGemma 2 (shared text and vision)") == (active == "unified")
assert enabled("Legacy text embeddings") == (active == "legacy")
assert enabled("Legacy image embeddings") == (active == "legacy")
dialog.close()
runtime._ai_pool.waitForDone(5000)
store.close()
'''
    result = subprocess.run(
        [sys.executable, "-c", script, active, str(tmp_path)],
        cwd=tmp_path,
        env=os.environ | {
            "QT_QPA_PLATFORM": "offscreen",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
