from __future__ import annotations

import asyncio

from sharespace.core.models import TransferStatus
from sharespace.networking.file_client import FileClient, FileClientError
from sharespace.networking.session_client import SessionClient, SessionClientError
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
    certificate_path = server.certificate_manager.certificate.private_key_path
    host = None
    client = None
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
        participant = server.manager.authenticate_participant(participant_token)
        server.file_service.select_root(tmp_path)
        host = SessionClient("127.0.0.1", server.port, session.certificate_fingerprint)
        client = FileClient(
            "127.0.0.1", server.port, participant_token,
            certificate_fingerprint=session.certificate_fingerprint,
        )

        async def transfer():
            state = await client.settings()
            assert state.available and state.uploads_enabled is False
            assert state.permission.value == "READ_ONLY"
            listing = await client.browse()
            assert "source.txt" in {item.filename for item in listing.items}
            downloaded = await client.download("source.txt", downloaded_path)
            await host.set_uploads_enabled(True, host_token)
            try:
                await client.upload(local_upload, "drop")
            except FileClientError:
                pass
            else:
                raise AssertionError("READ_ONLY participant uploaded a file.")
            await host.set_participant_permission(
                participant.participant_id, "READ_WRITE", host_token
            )
            uploaded = await client.upload(local_upload, "drop")
            await host.set_participant_permission(
                participant.participant_id, "READ_ONLY", host_token
            )
            try:
                await client.upload(local_upload, "drop")
            except FileClientError:
                pass
            else:
                raise AssertionError("Revoked READ_WRITE permission still allowed an upload.")
            await host.set_participant_permission(
                participant.participant_id, "READ_WRITE", host_token
            )
            await host.set_uploads_enabled(False, host_token)
            try:
                await client.upload(local_upload, "drop")
            except FileClientError:
                pass
            else:
                raise AssertionError("Global upload disable did not block an upload.")
            return downloaded, uploaded

        downloaded, uploaded = asyncio.run(transfer())

        assert downloaded.status is TransferStatus.COMPLETED
        assert uploaded.status is TransferStatus.COMPLETED
        assert downloaded_path.read_bytes() == b"source payload"
        assert (tmp_path / "drop" / "local-upload.txt").read_bytes() == b"participant payload"
        assert session.certificate_fingerprint is not None
        host.close()
    finally:
        if host is not None:
            host.close()
        if client is not None:
            client.close()
        try:
            server.manager.end_session(host_token)
        except Exception:
            pass
        server.stop()
    assert not certificate_path.exists()
