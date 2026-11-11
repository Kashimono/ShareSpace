from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

import sharespace.ui.session_join_dialog as session_join_module
from sharespace.ui.session_join_dialog import SessionJoinDialog


def _app():
    return QApplication.instance() or QApplication([])


class _DiscoveryStub:
    def __init__(self, on_change=None):
        self.active = False

    def sessions(self):
        return []

    def stop(self):
        self.active = False


def test_join_dialog_shows_discovery_empty_state_and_bounds_poll_failures(monkeypatch):
    _app()
    monkeypatch.setattr(session_join_module, "SessionDiscovery", _DiscoveryStub)
    dialog = SessionJoinDialog()
    dialog._discovery.active = True

    dialog._update_discovered_sessions()
    assert "No sessions found yet" in dialog._status.text()

    dialog._request_id = "request-id"
    dialog._request_token = "request-token"
    dialog._poll_timer.start()
    dialog._request_poll_failed("temporary connection issue")
    assert "1/3" in dialog._status.text()
    assert dialog._request_id == "request-id"
    assert dialog._poll_timer.isActive()

    dialog._request_poll_failed("temporary connection issue")
    assert "2/3" in dialog._status.text()
    dialog._request_poll_failed("temporary connection issue")

    assert "try again" in dialog._status.text()
    assert dialog._request_id is None
    assert dialog._request_token is None
    assert not dialog._poll_timer.isActive()
    dialog.close()
