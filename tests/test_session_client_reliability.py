from __future__ import annotations

import asyncio
import threading

import sharespace.networking.session_client as session_client_module
from sharespace.networking.session_client import (
    MAX_EVENT_RECONNECT_ATTEMPTS, SessionClient,
)


def test_event_reconnect_uses_distinct_states_and_stops_after_bounded_failures(monkeypatch):
    client = SessionClient.__new__(SessionClient)
    client.host = "192.168.1.20"
    client.port = 50001

    class Security:
        async def verify_peer(self):
            raise OSError("host unavailable")

    client.security = Security()
    states = []

    async def immediate_sleep(_delay):
        return None

    monkeypatch.setattr(session_client_module.asyncio, "sleep", immediate_sleep)
    asyncio.run(client.events(
        session_id="a" * 32,
        token="not persisted",
        stop_event=threading.Event(),
        on_event=lambda _event: None,
        participant=True,
        on_connection_state=states.append,
    ))
    assert states[-1] == "host_unavailable"
    assert states.count("disconnected") == MAX_EVENT_RECONNECT_ATTEMPTS
    assert all(left != right for left, right in zip(states, states[1:]))
    assert "verifying_tls" in states
    assert "connected" not in states
