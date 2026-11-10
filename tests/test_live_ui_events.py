"""Qt model-level checks for event-driven refresh and notification fallback."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QSystemTrayIcon, QVBoxLayout

from sharespace.ui.file_browser_dialog import FileBrowserDialog
from sharespace.ui.notification_manager import NotificationManager


class _ClientStub:
    def close(self):
        pass


def _application():
    return QApplication.instance() or QApplication([])


def test_file_browser_refreshes_only_affected_current_directory(monkeypatch):
    _application()
    browser = FileBrowserDialog(_ClientStub())
    browser._path = "Documents"
    browser._notifications._notify_native = lambda *_args: False
    requested = []
    monkeypatch.setattr(browser, "_queue_live_refresh", lambda: requested.append(browser._path))

    browser.handle_session_event({
        "type": "file_created",
        "event_id": "a" * 32,
        "data": {
            "filename": "paper.pdf",
            "relative_path": "Documents/paper.pdf",
            "parent_path": "Documents",
        },
    })
    browser.handle_session_event({
        "type": "file_modified",
        "event_id": "b" * 32,
        "data": {
            "filename": "other.txt",
            "relative_path": "Other/other.txt",
            "parent_path": "Other",
        },
    })

    assert requested == ["Documents"]
    assert "paper.pdf" in browser._notifications._banner.text()
    browser.close()


def test_file_browser_recovers_to_parent_when_current_folder_is_deleted(monkeypatch):
    _application()
    browser = FileBrowserDialog(_ClientStub())
    browser._path = "Documents/Old"
    requested = []
    monkeypatch.setattr(browser, "_queue_live_refresh", lambda: requested.append(browser._path))
    browser.handle_session_event({
        "type": "shared_directory_changed",
        "event_id": "c" * 32,
        "data": {
            "available": True,
            "directory_name": "shared",
            "relative_path": "Documents/Old",
            "change": "deleted",
        },
    })

    assert browser._path == "Documents"
    assert requested == ["Documents"]
    browser.close()


def test_notification_fallback_escapes_text_and_deduplicates(monkeypatch):
    _application()
    parent = QDialog()
    layout = QVBoxLayout(parent)
    notifications = NotificationManager(parent, layout)
    monkeypatch.setattr(notifications, "_notify_native", lambda *_args: False)

    assert notifications.notify("<New file>", "A & B.txt", event_id="d" * 32) is False
    rendered = notifications._banner.text()
    assert "&lt;New file&gt;" in rendered
    assert "A &amp; B.txt" in rendered
    assert notifications.notify("<New file>", "A & B.txt", event_id="d" * 32) is False
    assert notifications._banner.text() == rendered
    notifications.close()
    parent.close()


def test_native_notification_failure_uses_in_app_fallback(monkeypatch):
    _application()
    parent = QDialog()
    layout = QVBoxLayout(parent)
    notifications = NotificationManager(parent, layout)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    monkeypatch.setattr(QSystemTrayIcon, "supportsMessages", staticmethod(lambda: True))

    def fail_message(*_args):
        raise RuntimeError("platform notification unavailable")

    monkeypatch.setattr(QSystemTrayIcon, "showMessage", fail_message)
    assert notifications.notify("ShareSpace", "A file changed") is False
    assert not notifications._banner.isHidden()
    notifications.close()
    parent.close()
