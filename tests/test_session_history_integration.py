from __future__ import annotations

from datetime import datetime, timezone

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from sharespace.core.models import PublicSession, SessionStatus
from sharespace.networking.session_server import SessionServer
from sharespace.storage.local_store import LocalStore
from sharespace.ui.session_join_dialog import SessionJoinDialog


def _application():
    return QApplication.instance() or QApplication([])


def test_host_session_lifecycle_is_saved_after_server_success_and_end(tmp_path):
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    server = SessionServer(history_store=store)
    session, _joining_code, host_token = server.start(
        session_name="History test",
        host_device_name="Test host",
        host_os="Windows",
        host_ip="192.168.1.25",
    )
    assert server.history_record_id
    assert store.session_records() == []
    assert server.manager.host_token_valid(host_token)
    server.stop()
    history = store.session_records(role="HOST")
    assert len(history) == 1
    assert history[0]["session_id"] == session.session_id
    assert history[0]["session_name"] == "History test"
    assert history[0]["outcome"] == "COMPLETED"
    store.close()


def test_join_dialog_records_only_authenticated_sessions(tmp_path):
    _application()
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    dialog = SessionJoinDialog(store=store)
    dialog._selected_session = PublicSession(
        session_id="a" * 32,
        session_name="Join history",
        host_device_name="Nearby host",
        host_os="Linux",
        host_ip="192.168.1.31",
        port=52000,
        participant_count=2,
        status=SessionStatus.ACTIVE,
        created_at=datetime.now(timezone.utc),
        certificate_fingerprint="b" * 64,
    )
    assert store.session_records() == []
    dialog._on_session_event({
        "type": "authenticated", "participant_id": "peer-1", "permission": "READ_ONLY"
    })
    assert dialog._history_record_id is not None
    dialog._on_session_event({"type": "session_ended"})
    history = store.session_records(role="PARTICIPANT")
    assert len(history) == 1
    assert history[0]["host_device_name"] == "Nearby host"
    assert history[0]["outcome"] == "COMPLETED"
    assert "joining_code" not in history[0]
    dialog.close()
    store.close()


def test_manual_join_failure_is_recorded_without_host_address_or_credentials(tmp_path):
    _application()
    store = LocalStore(tmp_path / "sharespace.sqlite3")
    dialog = SessionJoinDialog(store=store)
    dialog._manual_selection = True

    dialog._record_failed_attempt()

    history = store.session_records(role="PARTICIPANT")
    assert len(history) == 1
    assert history[0]["outcome"] == "FAILED"
    assert history[0]["session_id"] is None
    assert history[0]["host_device_name"] == "Unknown host"
    assert "192.168." not in str(history[0])
    assert "token" not in str(history[0]).lower()
    dialog.close()
    store.close()
