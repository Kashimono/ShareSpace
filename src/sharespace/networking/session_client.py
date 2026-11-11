"""Asynchronous HTTP and WebSocket client for local session endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import threading
from collections.abc import Callable
from typing import Any

import httpx
import websockets

from sharespace.core.models import HostSnapshot, JoinRequestCreated, JoinRequestPoll, PublicSession
from sharespace.networking.session_server import API_PREFIX
from sharespace.networking.tls_security import (
    TLSConnectionSecurity,
    TLSVerificationError,
    format_fingerprint,
)
from sharespace.networking.websocket_events import RecentEventIds, validate_wire_event

logger = logging.getLogger(__name__)
MAX_EVENT_RECONNECT_ATTEMPTS = 12


class SessionClientError(RuntimeError):
    """A session request failed or returned an invalid response."""


class SessionClient:
    """Make local HTTP calls and hold an event WebSocket without Qt dependencies."""

    def __init__(
        self,
        host: str,
        port: int,
        certificate_fingerprint: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.base_url = f"https://{host}:{port}{API_PREFIX}"
        self.timeout_seconds = timeout_seconds
        self.security = TLSConnectionSecurity(host, port, certificate_fingerprint, timeout_seconds=timeout_seconds)
        self._http_client: httpx.Client | None = None
        self._http_client_lock = threading.Lock()

    @property
    def tls_version(self) -> str | None:
        return self.security.tls_version

    @property
    def certificate_verified(self) -> bool:
        return self.security.verified

    async def get_public_session(self) -> PublicSession:
        data = await self._request("GET", "/session")
        session = PublicSession.model_validate(data)
        if session.certificate_fingerprint != self.security.expected_fingerprint:
            raise SessionClientError("The host certificate changed. Verify the new fingerprint before continuing.")
        return session

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

    async def set_participant_permission(
        self,
        participant_id: str,
        permission: str,
        host_token: str,
    ) -> None:
        await self._request(
            "POST",
            f"/host/participants/{participant_id}/permissions",
            token=host_token,
            json={"permission": permission},
        )

    async def select_shared_root(self, path: str, host_token: str) -> None:
        await self._request("POST", "/host/files/root", token=host_token, json={"path": path})

    async def stop_sharing(self, host_token: str) -> None:
        await self._request("POST", "/host/files/stop", token=host_token)

    async def set_uploads_enabled(self, enabled: bool, host_token: str) -> None:
        await self._request(
            "POST", "/host/files/uploads", token=host_token, json={"enabled": enabled}
        )

    async def cancel_host_transfer(self, transfer_id: str, host_token: str) -> None:
        await self._request(
            "POST", f"/host/files/transfers/{transfer_id}/cancel", token=host_token
        )

    def close(self) -> None:
        with self._http_client_lock:
            client, self._http_client = self._http_client, None
        if client is not None:
            client.close()

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
        uri = f"wss://{self.host}:{self.port}{API_PREFIX}/sessions/{session_id}/events"
        retry_delay = 1.0
        attempts = 0
        last_state: str | None = None

        def emit_state(value: str) -> None:
            nonlocal last_state
            if on_connection_state is not None and value != last_state:
                last_state = value
                on_connection_state(value)

        recent_ids = RecentEventIds()
        while not stop_event.is_set():
            try:
                emit_state("verifying_tls")
                await self.security.verify_peer()
                emit_state("connecting")
                async with websockets.connect(
                    uri,
                    ssl=self.security.ssl_context,
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
                    attempts = 0
                    emit_state("connected")
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
                            if "event_id" in data:
                                try:
                                    event = validate_wire_event(data)
                                except (TypeError, ValueError):
                                    logger.warning("Ignored an invalid session event envelope.")
                                    continue
                                if not recent_ids.add(event.event_id):
                                    continue
                            on_event(data)
                            if data.get("type") == "session_ended":
                                return
            except TLSVerificationError as error:
                on_event({"type": "session_security_error", "detail": str(error)})
                emit_state("security_error")
                return
            except ssl.SSLCertVerificationError:
                replacement = await self.security.inspect_changed_fingerprint()
                if replacement is not None:
                    on_event({
                        "type": "session_security_error",
                        "detail": "The host certificate changed. Verify it again through a trusted channel.",
                        "presented_fingerprint": replacement,
                    })
                    emit_state("security_error")
                    return
                logger.warning("The verified session certificate failed TLS validation.")
                on_event({
                    "type": "session_security_error",
                    "detail": "The host certificate failed TLS validation.",
                })
                emit_state("security_error")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                emit_state("disconnected")
                logger.debug("Session event connection interrupted (%s).", type(error).__name__)
                if stop_event.is_set():
                    break
                attempts += 1
                if attempts >= MAX_EVENT_RECONNECT_ATTEMPTS:
                    emit_state("host_unavailable")
                    return
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 10.0)
        emit_state("stopped")

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
            await self.security.verify_peer()
            client = self._get_http_client()
            response = await asyncio.to_thread(
                client.request,
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json,
            )
        except TLSVerificationError as error:
            logger.warning("Session HTTPS certificate validation failed.")
            raise SessionClientError(str(error)) from error
        except httpx.HTTPError as error:
            replacement = await self.security.inspect_changed_fingerprint()
            if replacement is not None:
                logger.warning("Session HTTPS certificate changed during the active session.")
                raise SessionClientError(
                    "The host certificate changed. Presented fingerprint: "
                    f"{format_fingerprint(replacement)}"
                ) from error
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

    def _get_http_client(self) -> httpx.Client:
        with self._http_client_lock:
            if self._http_client is None:
                self._http_client = httpx.Client(
                    verify=self.security.ssl_context,
                    timeout=self.timeout_seconds,
                    trust_env=False,
                )
            return self._http_client
