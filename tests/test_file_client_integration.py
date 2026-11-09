from __future__ import annotations

import asyncio

from sharespace.core.models import TransferStatus
from sharespace.networking.file_client import FileClient
from sharespace.networking.session_server import SessionServer


def test_file_client_streams_download_and_upload_over_embedded_server(tmp_path):
    (tmp_path / "source.txt").write_bytes(b"source payload")
    local_upload = tmp_path / "local-upload.txt"
    local_upload.write_bytes(b"participant payload")
    (tmp_path / "drop").mkdir()
    downloaded_path = tmp_path / "downloaded.txt"

    server = SessionServer()
    session, joining_code, host_token = server.start(
        session_name="File integration",
        host_device_name="Host",
        host_os="Windows",
        host_ip="192.168.1.55",
    )
    try:
        request_id, request_token, _status = server.manager.request_join(
            session_id=session.session_id,
            client_id="file-peer",
            device_name="Participant",
            operating_system="Linux",
            joining_code=joining_code,
            source_address="192.168.1.56",
        )
        server.manager.approve_request(request_id, host_token)
        _status, participant_token = server.manager.poll_join_request(request_id, request_token)
        server.manager.authenticate_participant(participant_token)
        server.file_service.select_root(tmp_path)
        server.file_service.set_uploads_enabled(True)
        client = FileClient("127.0.0.1", server.port, participant_token)

        async def transfer():
            state = await client.settings()
            assert state.available and state.uploads_enabled
            listing = await client.browse()
            assert "source.txt" in {item.filename for item in listing.items}
            downloaded = await client.download("source.txt", downloaded_path)
            uploaded = await client.upload(local_upload, "drop")
            return downloaded, uploaded

        downloaded, uploaded = asyncio.run(transfer())

        assert downloaded.status is TransferStatus.COMPLETED
        assert uploaded.status is TransferStatus.COMPLETED
        assert downloaded_path.read_bytes() == b"source payload"
        assert (tmp_path / "drop" / "local-upload.txt").read_bytes() == b"participant payload"
    finally:
        try:
            server.manager.end_session(host_token)
        except Exception:
            pass
        server.stop()
