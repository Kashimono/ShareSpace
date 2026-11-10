"""Loopback integration check for the production Uvicorn and WebSocket stack."""

import asyncio
import json

import websockets
import httpx
import pytest

from sharespace.networking.session_client import SessionClient, SessionClientError
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
                if message.get("type") == "participant_disconnected":
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
