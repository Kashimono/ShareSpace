"""FastAPI session endpoints and an embeddable Uvicorn server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import ssl
import threading
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sharespace.core.file_manager import FileAccessError
from sharespace.core.models import (
    ApiError,
    HostSnapshot,
    JoinRequestCreate,
    JoinRequestCreated,
    JoinRequestPoll,
    JoinRequestStatus,
    ParticipantPermissionUpdate,
    PermissionLevel,
    PublicSession,
    TransferDirection,
)
from sharespace.core.session_manager import (
    InvalidCredential,
    InvalidTransition,
    PermissionDenied,
    SessionError,
    SessionManager,
    SessionUnavailable,
)
from sharespace.core.transfer_manager import TransferCancelled, TransferNotFound
from sharespace.networking.file_routes import create_file_router
from sharespace.networking.file_event_coordinator import FileEventCoordinator
from sharespace.networking.file_service import FileService
from sharespace.networking.tls_security import SessionCertificateManager
from sharespace.networking.websocket_events import EventBroker

logger = logging.getLogger(__name__)
API_PREFIX = "/api/v1"


def create_session_app(
    manager: SessionManager,
    *,
    event_broker: EventBroker | None = None,
    file_service: FileService | None = None,
    monitor_interval_seconds: float = 1.0,
) -> FastAPI:
    """Build an isolated app instance for one host session or an API test."""
    broker = event_broker or EventBroker()
    files = file_service or FileService()
    file_events = FileEventCoordinator(files, broker)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await file_events.start()

        async def monitor() -> None:
            while True:
                await asyncio.sleep(monitor_interval_seconds)
                before = {peer.participant_id for peer in manager.connected_participants()}
                for change in manager.expire_inactive():
                    if change != "participant_disconnected":
                        await broker.publish({"type": change})
                after = {peer.participant_id for peer in manager.connected_participants()}
                for participant_id in before - after:
                    peer = manager.participant_view(participant_id)
                    if peer is not None:
                        await broker.publish(_participant_activity_event("participant_disconnected", peer, manager))
                await files.cancel_disconnected_participants(manager.connected_participant_ids())

        monitor_task = asyncio.create_task(monitor(), name="sharespace-heartbeat-monitor")
        try:
            yield
        finally:
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            await file_events.close()

    app = FastAPI(
        title="ShareSpace Local Session API",
        version="0.6.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.session_manager = manager
    app.state.event_broker = broker
    app.state.file_service = files
    app.state.file_event_coordinator = file_events

    @app.exception_handler(FileAccessError)
    async def file_error_handler(_request: Request, error: FileAccessError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content=ApiError(detail=str(error), code=error.code).model_dump(),
        )

    @app.exception_handler(TransferNotFound)
    async def transfer_not_found_handler(_request: Request, _error: TransferNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Transfer not found.", "code": "transfer_not_found"})

    @app.exception_handler(TransferCancelled)
    async def transfer_cancelled_handler(_request: Request, error: TransferCancelled) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error), "code": "transfer_cancelled"})

    @app.exception_handler(SessionError)
    async def session_error_handler(_request: Request, error: SessionError) -> JSONResponse:
        logger.warning("Session authorization rejected: %s", error.code)
        if isinstance(error, InvalidCredential):
            status_code = 401
        elif isinstance(error, PermissionDenied):
            status_code = 403
        elif isinstance(error, SessionUnavailable):
            status_code = 404
        elif isinstance(error, InvalidTransition):
            status_code = 409
        else:
            status_code = 400
        payload = ApiError(detail=str(error), code=error.code)
        return JSONResponse(status_code=status_code, content=payload.model_dump())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, error: RequestValidationError) -> JSONResponse:
        # Exclude submitted values: a validation response must never echo the join code.
        issues = [
            {
                "location": list(item.get("loc", ())),
                "message": item.get("msg", "Invalid value."),
                "code": item.get("type", "invalid_value"),
            }
            for item in error.errors()
        ]
        return JSONResponse(status_code=422, content={"code": "invalid_request", "detail": issues})

    def bearer(authorization: str | None, *, host: bool = False) -> str:
        error_type = PermissionDenied if host else InvalidCredential
        if not authorization:
            raise error_type("Authorization is required.")
        scheme, _, value = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not value.strip():
            raise error_type("Authorization is invalid.")
        return value.strip()

    @app.get(f"{API_PREFIX}/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/session", response_model=PublicSession)
    async def public_session() -> PublicSession:
        return manager.get_public_session()

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/join-requests", response_model=JoinRequestCreated)
    async def create_join_request(session_id: str, data: JoinRequestCreate, request: Request) -> JoinRequestCreated:
        request_id, token, status = manager.request_join(
            session_id=session_id,
            client_id=data.client_id,
            device_name=data.device_name,
            operating_system=data.operating_system,
            joining_code=data.joining_code,
            source_address=request.client.host if request.client else "unknown",
        )
        await broker.publish({"type": "join_request_pending", "request_id": request_id})
        return JoinRequestCreated(request_id=request_id, request_token=token, status=status)

    @app.get(f"{API_PREFIX}/join-requests/{{request_id}}", response_model=JoinRequestPoll)
    async def poll_join_request(
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> JoinRequestPoll:
        status, participant_token = manager.poll_join_request(request_id, bearer(authorization))
        details = {
            JoinRequestStatus.PENDING: "Waiting for the host to review your request.",
            JoinRequestStatus.APPROVED: "The host approved your request.",
            JoinRequestStatus.REJECTED: "The host rejected your request.",
            JoinRequestStatus.EXPIRED: "The request expired before the host responded.",
        }
        return JoinRequestPoll(
            request_id=request_id,
            status=status,
            participant_token=participant_token if status is JoinRequestStatus.APPROVED else None,
            detail=details[status],
        )

    @app.get(f"{API_PREFIX}/host/snapshot", response_model=HostSnapshot)
    async def host_snapshot(authorization: str | None = Header(default=None)) -> HostSnapshot:
        session, pending, participants = manager.host_snapshot(bearer(authorization, host=True))
        return HostSnapshot(session=session, pending_requests=pending, participants=participants)

    @app.post(f"{API_PREFIX}/host/join-requests/{{request_id}}/approve")
    async def approve_request(
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        manager.approve_request(request_id, bearer(authorization, host=True))
        await broker.publish({"type": "join_request_approved", "request_id": request_id})
        return {"status": "approved", "request_id": request_id}

    @app.post(f"{API_PREFIX}/host/join-requests/{{request_id}}/reject")
    async def reject_request(
        request_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        manager.reject_request(request_id, bearer(authorization, host=True))
        await broker.publish({"type": "join_request_rejected", "request_id": request_id})
        return {"status": "rejected", "request_id": request_id}

    @app.post(f"{API_PREFIX}/host/participants/{{participant_id}}/disconnect")
    async def disconnect_participant(
        participant_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict[str, str]:
        host_token = bearer(authorization, host=True)
        async with files.authorization_gate:
            participant = manager.disconnect_participant(participant_id, host_token)
            files.transfers.cancel_active(participant_id=participant_id)
        logger.info("Participant disconnected by host: participant_id=%s", participant_id)
        await broker.publish(_participant_activity_event("participant_disconnected", participant, manager))
        return {"status": participant.status.value, "participant_id": participant_id}

    @app.post(f"{API_PREFIX}/host/participants/{{participant_id}}/permissions")
    async def set_participant_permission(
        participant_id: str,
        data: ParticipantPermissionUpdate,
        authorization: str | None = Header(default=None),
    ):
        host_token = bearer(authorization, host=True)
        async with files.authorization_gate:
            before = next(
                (
                    item for item in manager.host_snapshot(host_token)[2]
                    if item.participant_id == participant_id
                ),
                None,
            )
            if before is None or before.role.value != "PARTICIPANT":
                raise SessionUnavailable("Participant not found.")
            participant = manager.set_participant_permission(
                participant_id, data.permission, host_token
            )
            if (
                before.permission is PermissionLevel.READ_WRITE
                and data.permission is PermissionLevel.READ_ONLY
            ):
                files.transfers.cancel_active(
                    direction=TransferDirection.UPLOAD,
                    participant_id=participant_id,
                )
        logger.info(
            "Participant permission changed: participant_id=%s permission=%s",
            participant_id,
            participant.permission.value if participant.permission else "none",
        )
        await broker.publish(
            {
                "type": "participant_permission_changed",
                "participant_id": participant_id,
                "permission": participant.permission.value if participant.permission else None,
            },
            target_participant_id=participant_id,
        )
        return participant

    @app.post(f"{API_PREFIX}/host/end")
    async def end_session(authorization: str | None = Header(default=None)) -> dict[str, str]:
        host_token = bearer(authorization, host=True)
        async with files.authorization_gate:
            session = manager.end_session(host_token)
            files.transfers.cancel_active()
            files.set_uploads_enabled(False)
        await file_events.stop()
        await broker.publish({"type": "session_ended", "session_id": session.session_id})
        return {"status": "ended", "session_id": session.session_id}

    @app.websocket(f"{API_PREFIX}/sessions/{{session_id}}/events")
    async def session_events(websocket: WebSocket, session_id: str) -> None:
        await websocket.accept()
        queue = broker.subscribe()
        participant_id: str | None = None
        participant_claimed = False
        try:
            try:
                auth = await asyncio.wait_for(websocket.receive_json(), timeout=8)
            except TimeoutError:
                await websocket.close(code=1008, reason="Authentication timed out.")
                return
            if not isinstance(auth, dict) or auth.get("type") != "AUTHENTICATE":
                await websocket.close(code=1008, reason="First message must authenticate.")
                return
            token = auth.get("token")
            if not isinstance(token, str) or not token:
                await websocket.close(code=1008, reason="Credentials are required.")
                return
            session = manager.get_public_session()
            if session.session_id != session_id:
                await websocket.close(code=1008, reason="Session is unavailable.")
                return
            if manager.host_token_valid(token):
                role = "host"
                permission = None
            else:
                participant, transitioned = manager.authenticate_participant_with_transition(token)
                participant_id = participant.participant_id
                permission = participant.permission.value if participant.permission else None
                if not broker.claim_participant(participant_id):
                    await websocket.close(code=1008, reason="This participant is already connected.")
                    return
                participant_claimed = True
                broker.bind_participant(queue, participant_id)
                role = "participant"
            directory_state = files.state()
            await websocket.send_json(
                {
                    "type": "authenticated",
                    "role": role.upper(),
                    "participant_id": participant_id,
                    "permission": permission,
                    "heartbeat_interval_seconds": 5,
                    "shared_directory": {
                        "available": directory_state.available,
                        "uploads_enabled": directory_state.uploads_enabled,
                        "directory_name": directory_state.directory_name,
                    },
                }
            )
            if participant_id and transitioned:
                await broker.publish(
                    _participant_activity_event("participant_connected", participant, manager),
                )
            last_heartbeat = time.monotonic()
            while True:
                try:
                    incoming: Any = await asyncio.wait_for(websocket.receive_json(), timeout=1)
                except TimeoutError:
                    incoming = None
                if incoming is not None:
                    if not isinstance(incoming, dict) or incoming.get("type") != "HEARTBEAT":
                        await websocket.send_json({"type": "error", "code": "unsupported_message"})
                    elif participant_id:
                        manager.heartbeat(token)
                        last_heartbeat = time.monotonic()
                        await websocket.send_json({"type": "heartbeat_ack"})
                if participant_id and time.monotonic() - last_heartbeat > 35:
                    await websocket.close(code=4001, reason="Heartbeat timeout.")
                    return
                while not queue.empty():
                    event = queue.get_nowait()
                    if event.get("type") == "session_ended":
                        # Session termination is the final control event for all
                        # peers, including credentials revoked by that operation.
                        await websocket.send_json(event)
                        await websocket.close(code=4000, reason="Host ended the session.")
                        return
                    if participant_id and not manager.participant_is_connected(participant_id):
                        # A final control message contains no event payload. Never
                        # deliver queued session data after the credential is revoked.
                        await websocket.send_json({"type": "error", "code": "participant_disconnected"})
                        await websocket.close(code=4003, reason="Participant access was revoked.")
                        return
                    await websocket.send_json(event)
        except SessionError as error:
            logger.warning("Session WebSocket authorization rejected: %s", error.code)
            with contextlib.suppress(Exception):
                await websocket.send_json({"type": "error", "code": "unauthorized"})
                await websocket.close(code=1008, reason="Credentials are invalid or expired.")
        except WebSocketDisconnect:
            pass
        except Exception as error:
            logger.warning("Session WebSocket failed (%s).", type(error).__name__)
            with contextlib.suppress(Exception):
                await websocket.close(code=1011, reason="Session connection failed.")
        finally:
            broker.unsubscribe(queue)
            if participant_id and participant_claimed:
                async with files.authorization_gate:
                    participant = manager.participant_disconnected(participant_id)
                    files.transfers.cancel_active(participant_id=participant_id)
                broker.release_participant(participant_id)
                if participant is not None:
                    logger.info("Participant WebSocket disconnected: participant_id=%s", participant_id)
                    await broker.publish(_participant_activity_event("participant_disconnected", participant, manager))

    app.include_router(create_file_router(
        manager,
        files,
        event_broker=broker,
        root_changed=file_events.set_root,
    ))
    return app


def _participant_activity_event(event_type: str, participant, manager: SessionManager) -> dict[str, object]:
    """Build a non-secret host/participant transition event."""
    session = manager.get_public_session()
    return {
        "type": event_type,
        "participant_id": participant.participant_id,
        "device_name": participant.device_name,
        "operating_system": participant.operating_system,
        "participant_count": session.participant_count,
    }


class SessionServer:
    """Run one FastAPI session server in a background thread on an ephemeral port."""

    def __init__(
        self,
        manager: SessionManager | None = None,
        *,
        ready_timeout: float = 10.0,
        certificate_manager: SessionCertificateManager | None = None,
    ) -> None:
        self.manager = manager or SessionManager()
        self.file_service = FileService()
        self.certificate_manager = certificate_manager or SessionCertificateManager()
        self.ready_timeout = ready_timeout
        self._server: uvicorn.Server | None = None
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._host_token: str | None = None
        self._port: int | None = None

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def host_token(self) -> str | None:
        return self._host_token

    def start(
        self,
        *,
        session_name: str,
        host_device_name: str,
        host_os: str,
        host_ip: str,
        host_addresses: tuple[str, ...] | list[str] | None = None,
    ) -> tuple[PublicSession, str, str]:
        """Bind a random local port, create the session, and start Uvicorn."""
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("This session server is already running.")
        bind_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        bind_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        credentials = None
        try:
            bind_socket.bind(("0.0.0.0", 0))
            bind_socket.listen(128)
            bind_socket.setblocking(False)
            port = int(bind_socket.getsockname()[1])
            addresses = list(host_addresses or ())
            if host_ip not in addresses:
                addresses.insert(0, host_ip)
            certificate = self.certificate_manager.generate(addresses)
            session, credentials = self.manager.create_session(
                session_name=session_name,
                host_device_name=host_device_name,
                host_os=host_os,
                host_ip=host_ip,
                port=port,
                certificate_fingerprint=certificate.fingerprint,
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    create_session_app(self.manager, file_service=self.file_service),
                    host="0.0.0.0",
                    port=port,
                    log_level="warning",
                    access_log=False,
                    lifespan="on",
                    timeout_graceful_shutdown=2,
                    ssl_certfile=str(certificate.certificate_path),
                    ssl_keyfile=str(certificate.private_key_path),
                    ssl_context_factory=_tls_server_context_factory,
                )
            )
            thread = threading.Thread(
                target=server.run,
                kwargs={"sockets": [bind_socket]},
                name="sharespace-session-server",
                daemon=True,
            )
            self._socket = bind_socket
            self._server = server
            self._thread = thread
            self._port = port
            self._host_token = credentials.host_token
            thread.start()
            deadline = time.monotonic() + self.ready_timeout
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.025)
            if not server.started:
                self.stop()
                self.manager.end_session(credentials.host_token)
                raise RuntimeError("The local session server did not start.")
            return session, credentials.joining_code, credentials.host_token
        except Exception as error:
            logger.error("Local TLS session startup failed (%s).", type(error).__name__)
            if credentials is not None:
                with contextlib.suppress(Exception):
                    self.manager.end_session(credentials.host_token)
            self.file_service.cleanup()
            self.certificate_manager.cleanup()
            with contextlib.suppress(OSError):
                bind_socket.close()
            if self._thread is None or not self._thread.is_alive():
                self._socket = None
            raise

    def stop(self) -> None:
        """Ask Uvicorn to stop and release its listening socket."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._thread is not None and self._thread.is_alive():
            logger.error("Session server did not stop within the cleanup timeout.")
        elif self._socket is not None:
            with contextlib.suppress(OSError):
                self._socket.close()
            self._socket = None
        self._server = None
        self._thread = None
        self._port = None
        self._host_token = None
        self.file_service.stop_sharing()
        self.file_service.cleanup()
        self.certificate_manager.cleanup()


def _tls_server_context_factory(_config, default_factory):
    context = default_factory()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
