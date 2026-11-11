from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sharespace.storage.local_store import LocalStore
from sharespace.ui.app_settings import load_preferences
from sharespace.ui.main_window import MainWindow


def test_main_window_starts_with_settings_and_history_actions(tmp_path):
    application = QApplication.instance() or QApplication([])
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    window = MainWindow(store=store, preferences=load_preferences(store))
    window.show()
    application.processEvents()
    assert window.isVisible()
    assert window._settings_button.isEnabled()
    assert window._transfer_history_button.isEnabled()
    assert window._session_history_button.isEnabled()
    window.close()
    store.close()
