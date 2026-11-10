"""Loopback integration check for the production Uvicorn and WebSocket stack."""

import asyncio
import json

import websockets
import httpx
import pytest

from sharespace.networking.session_client import SessionClient, SessionClientError
from sharespace.networking.file_client import FileClient
from sharespace.networking.session_server import API_PREFIX, SessionServer


def test_embedded_server_supports_http_approval_websocket_heartbeat_and_teardown():
    server = SessionServer()
    session, joining_code, host_token = server.start(
        session_name="Loopback room",
        host_device_name="Host test",
        host_os="Windows",
        host_ip="192.168.1.25",
    )
    certificate_path = server.certificate_manager.certificate.private_key_path
    host = SessionClient("127.0.0.1", session.port, session.certificate_fingerprint)
    peer = SessionClient("127.0.0.1", session.port, session.certificate_fingerprint)

    async def exercise():
        created = await peer.request_join(
            session_id=session.session_id,
            client_id="loopback-peer",
            device_name="Peer test",
            operating_system="Linux",
            joining_code=joining_code,
        )
        assert peer.base_url.startswith("https://")
        assert peer.security.verified
        snapshot = await host.host_snapshot(host_token)
        request_id = snapshot.pending_requests[0].request_id
        await host.approve_request(request_id, host_token)
        poll = await peer.poll_join_request(request_id, created.request_token)
        assert poll.participant_token

        await peer.security.verify_peer()
        uri = f"wss://127.0.0.1:{session.port}{API_PREFIX}/sessions/{session.session_id}/events"
        async with websockets.connect(
            uri, ssl=peer.security.ssl_context, open_timeout=5
        ) as websocket:
            await websocket.send(json.dumps({"type": "AUTHENTICATE", "token": poll.participant_token}))
            authenticated = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            assert authenticated["type"] == "authenticated"
            assert authenticated["role"] == "PARTICIPANT"
            participant_id = authenticated["participant_id"]
            assert authenticated["permission"] == "READ_ONLY"
            joined = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            assert joined["type"] == "participant_connected"
            assert joined["event_type"] == "PARTICIPANT_JOINED"
            assert joined["data"]["participant_id"] == participant_id
            assert len(joined["event_id"]) == 32

            await host.set_participant_permission(participant_id, "READ_WRITE", host_token)
            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                if message.get("type") == "participant_permission_changed":
                    assert message["permission"] == "READ_WRITE"
                    break

            await websocket.send(json.dumps({"type": "HEARTBEAT"}))
            seen = {"participant_connected"}
            while not {"heartbeat_ack", "participant_connected"}.issubset(seen):
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                seen.add(message["type"])
            assert "participant_connected" in seen

            await host.disconnect_participant(participant_id, host_token)
            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                if message.get("type") == "error":
                    assert message["code"] == "participant_disconnected"
                    break

        try:
            await peer._request("GET", "/files/settings", token=poll.participant_token)
        except SessionClientError:
            pass
        else:
            raise AssertionError("Disconnected participant credentials remained valid.")

        wrong_pin = SessionClient("127.0.0.1", session.port, "0" * 64)
        try:
            await wrong_pin.get_public_session()
        except SessionClientError as error:
            assert "fingerprint mismatch" in str(error).lower()
            assert "presented fingerprint" in str(error).lower()
        else:
            raise AssertionError("A mismatched certificate fingerprint was accepted.")
        assert await wrong_pin.security.inspect_changed_fingerprint() == session.certificate_fingerprint
        wrong_pin.close()

        with pytest.raises(httpx.HTTPError):
            httpx.get(f"http://127.0.0.1:{session.port}{API_PREFIX}/health", timeout=2)

        await host.end_session(host_token)
        try:
            await peer.get_public_session()
        except SessionClientError:
            return
        raise AssertionError("The ended session was still available over HTTP.")

    try:
        asyncio.run(exercise())
    finally:
        host.close()
        peer.close()
        server.stop()
    assert not certificate_path.exists()


def test_embedded_server_broadcasts_one_event_after_completed_upload(tmp_path):
    server = SessionServer()
    session, joining_code, host_token = server.start(
        session_name="File events",
        host_device_name="Host test",
        host_os="Windows",
        host_ip="192.168.1.25",
    )
    host = SessionClient("127.0.0.1", session.port, session.certificate_fingerprint)
    peer = SessionClient("127.0.0.1", session.port, session.certificate_fingerprint)
    shared = tmp_path / "shared"
    shared.mkdir()
    source = tmp_path / "completed.txt"
    source.write_text("committed upload", encoding="utf-8")

    async def exercise():
        created = await peer.request_join(
            session_id=session.session_id,
            client_id="file-event-peer",
            device_name="Peer test",
            operating_system="Linux",
            joining_code=joining_code,
        )
        snapshot = await host.host_snapshot(host_token)
        request_id = snapshot.pending_requests[0].request_id
        await host.approve_request(request_id, host_token)
        approval = await peer.poll_join_request(request_id, created.request_token)
        await host.select_shared_root(str(shared), host_token)
        await host.set_uploads_enabled(True, host_token)
        peer_file_client = FileClient(
            "127.0.0.1", session.port, approval.participant_token,
            security=peer.security,
        )
        try:
            uri = f"wss://127.0.0.1:{session.port}{API_PREFIX}/sessions/{session.session_id}/events"
            await peer.security.verify_peer()
            async with websockets.connect(uri, ssl=peer.security.ssl_context, open_timeout=5) as websocket:
                await websocket.send(json.dumps({"type": "AUTHENTICATE", "token": approval.participant_token}))
                authenticated = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                assert authenticated["shared_directory"]["available"] is True
                participant_id = authenticated["participant_id"]
                await host.set_participant_permission(participant_id, "READ_WRITE", host_token)
                while True:
                    message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                    if message.get("type") == "participant_permission_changed":
                        break
                transfer = await peer_file_client.upload(source)
                assert transfer.status.value == "COMPLETED"
                found_events = []
                deadline = asyncio.get_running_loop().time() + 6
                while asyncio.get_running_loop().time() < deadline:
                    message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=6))
                    if message.get("type") == "file_created":
                        found_events.append(message)
                        break
                assert len(found_events) == 1
                event = found_events[0]
                assert event["event_type"] == "FILE_CREATED"
                assert event["data"]["relative_path"] == "completed.txt"
                assert event["data"]["size_bytes"] == len("committed upload")
                assert str(shared) not in json.dumps(event)
                listing = await peer_file_client.browse("")
                assert [item.filename for item in listing.items] == ["completed.txt"]
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(websocket.recv(), timeout=0.4)

            deadline = asyncio.get_running_loop().time() + 3
            while asyncio.get_running_loop().time() < deadline:
                if server.manager.participant_view(participant_id).status.value == "DISCONNECTED":
                    break
                await asyncio.sleep(0.02)
            async with websockets.connect(uri, ssl=peer.security.ssl_context, open_timeout=5) as resumed:
                await resumed.send(json.dumps({"type": "AUTHENTICATE", "token": approval.participant_token}))
                reconnected = json.loads(await asyncio.wait_for(resumed.recv(), timeout=5))
                assert reconnected["type"] == "authenticated"
                assert reconnected["shared_directory"]["available"] is True
                listing = await peer_file_client.browse("")
                assert [item.filename for item in listing.items] == ["completed.txt"]
        finally:
            peer_file_client.close()

    try:
        asyncio.run(exercise())
    finally:
        host.close()
        peer.close()
        server.stop()
