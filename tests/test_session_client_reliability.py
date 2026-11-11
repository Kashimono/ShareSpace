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

    async def immediate_retry_wait(_stop_event, _delay):
        return None

    monkeypatch.setattr(
        session_client_module, "_wait_for_retry_or_stop", immediate_retry_wait
    )
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


def test_event_retry_wait_returns_immediately_when_shutdown_is_requested():
    stop_event = threading.Event()

    async def check_wait():
        task = asyncio.create_task(
            session_client_module._wait_for_retry_or_stop(stop_event, 30.0)
        )
        await asyncio.sleep(0.01)
        stop_event.set()
        await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(check_wait())
