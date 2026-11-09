"""Asynchronous HTTP and WebSocket client for local session endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from sharespace.core.models import HostSnapshot, JoinRequestCreated, JoinRequestPoll, PublicSession
from sharespace.networking.session_server import API_PREFIX

logger = logging.getLogger(__name__)


class SessionClientError(RuntimeError):
    """A session request failed or returned an invalid response."""


class SessionClient:
    """Make local HTTP calls and hold an event WebSocket without Qt dependencies."""

    def __init__(self, host: str, port: int, *, timeout_seconds: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}{API_PREFIX}"
        self.timeout_seconds = timeout_seconds

    async def get_public_session(self) -> PublicSession:
        data = await self._request("GET", "/session")
        return PublicSession.model_validate(data)

    async def request_join(
        self,
        *,
        session_id: str,
        client_id: str,
        device_name: str,
        operating_system: str,
        joining_code: str,
    ) -> JoinRequestCreated:
        payload = {
            "client_id": client_id,
            "device_name": device_name,
            "operating_system": operating_system,
            "joining_code": joining_code,
        }
        data = await self._request("POST", f"/sessions/{session_id}/join-requests", json=payload)
        return JoinRequestCreated.model_validate(data)

    async def poll_join_request(self, request_id: str, request_token: str) -> JoinRequestPoll:
        data = await self._request("GET", f"/join-requests/{request_id}", token=request_token)
        return JoinRequestPoll.model_validate(data)

    async def host_snapshot(self, host_token: str) -> HostSnapshot:
        data = await self._request("GET", "/host/snapshot", token=host_token)
        return HostSnapshot.model_validate(data)

    async def approve_request(self, request_id: str, host_token: str) -> None:
        await self._request("POST", f"/host/join-requests/{request_id}/approve", token=host_token)

    async def reject_request(self, request_id: str, host_token: str) -> None:
        await self._request("POST", f"/host/join-requests/{request_id}/reject", token=host_token)

    async def disconnect_participant(self, participant_id: str, host_token: str) -> None:
        await self._request("POST", f"/host/participants/{participant_id}/disconnect", token=host_token)

    async def end_session(self, host_token: str) -> None:
        await self._request("POST", "/host/end", token=host_token)

    async def events(
        self,
        *,
        session_id: str,
        token: str,
        stop_event: Any,
        on_event: Callable[[dict[str, Any]], None],
        participant: bool,
        on_connection_state: Callable[[str], None] | None = None,
    ) -> None:
        """Reconnect and consume events; participant clients send heartbeats."""
        uri = f"ws://{self.host}:{self.port}{API_PREFIX}/sessions/{session_id}/events"
        retry_delay = 1.0
        while not stop_event.is_set():
            try:
                if on_connection_state:
                    on_connection_state("connecting")
                async with websockets.connect(
                    uri,
                    ping_interval=20,
                    open_timeout=self.timeout_seconds,
                    close_timeout=2,
                    max_size=64 * 1024,
                ) as websocket:
                    await websocket.send(json.dumps({"type": "AUTHENTICATE", "token": token}))
                    auth = json.loads(await asyncio.wait_for(websocket.recv(), timeout=5))
                    if not isinstance(auth, dict) or auth.get("type") != "authenticated":
                        raise SessionClientError("Session WebSocket authentication failed.")
                    on_event(auth)
                    retry_delay = 1.0
                    if on_connection_state:
                        on_connection_state("connected")
                    last_heartbeat = asyncio.get_running_loop().time()
                    while not stop_event.is_set():
                        now = asyncio.get_running_loop().time()
                        if participant and now - last_heartbeat >= 5:
                            await websocket.send(json.dumps({"type": "HEARTBEAT"}))
                            last_heartbeat = now
                        try:
                            raw = await asyncio.wait_for(websocket.recv(), timeout=1)
                        except TimeoutError:
                            continue
                        data = json.loads(raw)
                        if isinstance(data, dict):
                            on_event(data)
                            if data.get("type") == "session_ended":
                                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if on_connection_state:
                    on_connection_state("disconnected")
                logger.debug("Session event connection interrupted: %s", error)
                if stop_event.is_set():
                    break
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
        if on_connection_state:
            on_connection_state("stopped")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json,
                )
        except httpx.HTTPError as error:
            raise SessionClientError("Could not reach the ShareSpace session host.") from error
        try:
            data = response.json()
        except ValueError as error:
            raise SessionClientError("The session host returned an invalid response.") from error
        if response.is_error:
            if isinstance(data, dict):
                detail = data.get("detail", "The session request failed.")
                if isinstance(detail, list):
                    detail = "; ".join(
                        str(item.get("message", "Invalid request.")) for item in detail
                    )
                raise SessionClientError(str(detail))
            raise SessionClientError("The session request failed.")
        if not isinstance(data, dict):
            raise SessionClientError("The session host returned an invalid response.")
        return data
