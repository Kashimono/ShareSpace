"""Loopback integration check for the production Uvicorn and WebSocket stack."""

import asyncio
import json

import websockets

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
    host = SessionClient("127.0.0.1", session.port)
    peer = SessionClient("127.0.0.1", session.port)

    async def exercise():
        created = await peer.request_join(
            session_id=session.session_id,
            client_id="loopback-peer",
            device_name="Peer test",
            operating_system="Linux",
            joining_code=joining_code,
        )
        snapshot = await host.host_snapshot(host_token)
        request_id = snapshot.pending_requests[0].request_id
        await host.approve_request(request_id, host_token)
        poll = await peer.poll_join_request(request_id, created.request_token)
        assert poll.participant_token

        uri = f"ws://127.0.0.1:{session.port}{API_PREFIX}/sessions/{session.session_id}/events"
        async with websockets.connect(uri, open_timeout=5) as websocket:
            await websocket.send(json.dumps({"type": "AUTHENTICATE", "token": poll.participant_token}))
            authenticated = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
            assert authenticated["type"] == "authenticated"
            assert authenticated["role"] == "PARTICIPANT"
            participant_id = authenticated["participant_id"]

            await websocket.send(json.dumps({"type": "HEARTBEAT"}))
            seen = set()
            while not {"heartbeat_ack", "participant_connected"}.issubset(seen):
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                seen.add(message["type"])
            assert "participant_connected" in seen

            await host.disconnect_participant(participant_id, host_token)
            while True:
                message = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                if message.get("type") == "participant_disconnected":
                    break

        await host.end_session(host_token)
        try:
            await peer.get_public_session()
        except SessionClientError:
            return
        raise AssertionError("The ended session was still available over HTTP.")

    try:
        asyncio.run(exercise())
    finally:
        server.stop()
