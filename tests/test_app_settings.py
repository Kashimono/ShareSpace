from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication

from sharespace.storage.local_store import LocalStore
from sharespace.ui.app_settings import apply_theme, default_preferences, load_preferences
from sharespace.ui.error_messages import user_facing_error


def _application():
    return QApplication.instance() or QApplication([])


def test_default_settings_and_persisted_settings_are_validated(tmp_path):
    _application()
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    defaults = default_preferences()
    assert load_preferences(store) == defaults
    store.set_setting("theme", "unexpected")
    store.set_setting("download_directory", str(tmp_path / "removed"))
    store.set_setting("history_retention_days", True)
    settings = load_preferences(store)
    assert settings["theme"] == "system"
    assert settings["download_directory"] == defaults["download_directory"]
    assert settings["history_retention_days"] == 365
    store.close()


def test_theme_changes_and_system_theme_can_be_restored():
    application = _application()
    apply_theme(application, "dark")
    assert application.palette().color(QPalette.ColorRole.Window).name() == "#1e232b"
    apply_theme(application, "light")
    assert application.palette().color(QPalette.ColorRole.Window).name() == "#f5f7fb"
    apply_theme(application, "system")
    assert application.styleSheet() == ""


def test_error_messages_hide_machine_paths_and_map_storage_failures():
    message = user_facing_error(OSError(r"Could not access C:\Users\aronr\Private\secret.dat"))
    assert "C:\\Users" not in message
    assert "secret.dat" not in message
    assert "not enough free space" in user_facing_error(OSError(28, "disk full")).casefold()
