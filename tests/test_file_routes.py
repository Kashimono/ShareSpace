from __future__ import annotations

import base64
import uuid

from fastapi.testclient import TestClient

from sharespace.core.models import TransferDirection
from sharespace.core.session_manager import SessionManager
from sharespace.networking.session_server import create_session_app


def make_authenticated_app(tmp_path):
    manager = SessionManager()
    session, credentials = manager.create_session(
        session_name="Files room",
        host_device_name="Host PC",
        host_os="Windows",
        host_ip="192.168.1.20",
        port=55001,
    )
    request_id, request_token, _status = manager.request_join(
        session_id=session.session_id,
        client_id="peer-1",
        device_name="Peer",
        operating_system="Linux",
        joining_code=credentials.joining_code,
        source_address="192.168.1.21",
    )
    manager.approve_request(request_id, credentials.host_token)
    _status, participant_token = manager.poll_join_request(request_id, request_token)
    manager.authenticate_participant(participant_token)
    app = create_session_app(manager)
    app.state.file_service.select_root(tmp_path)
    return manager, app, credentials, participant_token


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_file_routes_require_connected_participant_and_keep_root_private(tmp_path):
    (tmp_path / "readme.txt").write_text("share me", encoding="utf-8")
    manager, app, credentials, participant_token = make_authenticated_app(tmp_path)

    with TestClient(app) as client:
        assert client.get("/api/v1/files").status_code == 401
        assert client.get("/api/v1/files", headers=auth(credentials.host_token)).status_code == 401
        listing = client.get("/api/v1/files", headers=auth(participant_token))
        assert listing.status_code == 200
        assert listing.json()["shared_directory_name"] == tmp_path.name
        assert str(tmp_path) not in listing.text
        participant_id = next(
            item.participant_id
            for item in manager.host_snapshot(credentials.host_token)[2]
            if item.role.value == "PARTICIPANT"
        )
        transfer = app.state.file_service.transfers.create(
            filename="pending.txt",
            direction=TransferDirection.UPLOAD,
            total=12,
            participant_id=participant_id,
        )
        participant_transfers = client.get("/api/v1/files/transfers", headers=auth(participant_token))
        assert [item["transfer_id"] for item in participant_transfers.json()] == [transfer.transfer_id]
        cancelled = client.post(
            f"/api/v1/files/transfers/{transfer.transfer_id}/cancel",
            headers=auth(participant_token),
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "CANCELLED"

        manager.participant_disconnected(participant_id)
        disconnected = client.get("/api/v1/files", headers=auth(participant_token))
        assert disconnected.status_code == 401


def test_browse_download_metadata_and_server_side_size_limit(tmp_path):
    (tmp_path / "guide.txt").write_text("hello from host", encoding="utf-8")
    (tmp_path / ".hidden").write_text("private")
    (tmp_path / "too-large.bin").write_bytes(b"")
    with (tmp_path / "too-large.bin").open("r+b") as stream:
        stream.truncate(20_000_001)
    manager, app, _credentials, token = make_authenticated_app(tmp_path)

    with TestClient(app) as client:
        listing = client.get("/api/v1/files", headers=auth(token))
        assert [item["filename"] for item in listing.json()["items"]] == ["guide.txt", "too-large.bin"]
        metadata = client.get("/api/v1/files/info", params={"path": "guide.txt"}, headers=auth(token))
        assert metadata.status_code == 200
        assert metadata.json()["size_bytes"] == len("hello from host")

        identifier = uuid.uuid4().hex
        downloaded = client.get(
            "/api/v1/files/download",
            params={"path": "guide.txt"},
            headers={**auth(token), "X-Transfer-ID": identifier},
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"hello from host"
        assert downloaded.headers["x-transfer-id"] == identifier
        assert "filename*=UTF-8''guide.txt" in downloaded.headers["content-disposition"]
        assert downloaded.headers["x-content-type-options"] == "nosniff"

        too_large = client.get(
            "/api/v1/files/download", params={"path": "too-large.bin"}, headers=auth(token)
        )
        assert too_large.status_code == 413


def test_upload_default_off_enable_size_path_and_collision_protection(tmp_path):
    (tmp_path / "drop").mkdir()
    manager, app, credentials, token = make_authenticated_app(tmp_path)
    participant = auth(token)
    host = auth(credentials.host_token)

    with TestClient(app) as client:
        setting = client.get("/api/v1/files/settings", headers=participant)
        assert setting.status_code == 200
        assert setting.json()["uploads_enabled"] is False

        encoded_filename = base64.urlsafe_b64encode("résumé.txt".encode()).decode().rstrip("=")
        payload = "new local file".encode()
        upload_headers = {
            **participant,
            "X-Filename": encoded_filename,
            "X-File-Size": str(len(payload)),
            "X-Transfer-ID": uuid.uuid4().hex,
            "Content-Type": "application/octet-stream",
        }
        disabled = client.post(
            "/api/v1/files/upload", params={"path": "drop"}, headers=upload_headers, content=payload
        )
        assert disabled.status_code == 403

        enabled = client.post("/api/v1/host/files/uploads", headers=host, json={"enabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["uploads_enabled"] is True
        uploaded = client.post(
            "/api/v1/files/upload", params={"path": "drop"}, headers=upload_headers, content=payload
        )
        assert uploaded.status_code == 200, uploaded.text
        assert (tmp_path / "drop" / "résumé.txt").read_bytes() == payload

        collision = client.post(
            "/api/v1/files/upload", params={"path": "drop"}, headers=upload_headers, content=payload
        )
        assert collision.status_code == 409
        assert (tmp_path / "drop" / "résumé.txt").read_bytes() == payload

        traversal = client.get("/api/v1/files", params={"path": "../"}, headers=participant)
        assert traversal.status_code == 403
        oversized = client.post(
            "/api/v1/files/upload",
            headers={
                **participant,
                "X-Filename": encoded_filename,
                "X-File-Size": "20000001",
                "Content-Type": "application/octet-stream",
            },
            content=b"",
        )
        assert oversized.status_code == 413

        host_state = client.get("/api/v1/host/files/state", headers=host)
        assert host_state.status_code == 200
        assert str(tmp_path) not in host_state.text
        assert client.get("/api/v1/host/files/state", headers=participant).status_code == 403
