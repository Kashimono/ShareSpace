"""In-memory session state machine and credential checks.

This module has no network or Qt dependencies. The HTTP and WebSocket layers call
these methods, and tests can exercise the same rules without opening sockets.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sharespace.core.models import (
    JoinRequestStatus,
    JoinRequestView,
    PermissionLevel,
    ParticipantRole,
    ParticipantStatus,
    ParticipantView,
    PublicSession,
    SessionStatus,
)

JOIN_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
JOIN_CODE_LENGTH = 12
JOIN_CODE_PBKDF2_ROUNDS = 120_000


class SessionError(RuntimeError):
    """Base exception translated to a structured API error."""

    code = "session_error"


class SessionUnavailable(SessionError):
    code = "session_unavailable"


class InvalidCredential(SessionError):
    code = "invalid_credential"


class PermissionDenied(SessionError):
    code = "permission_denied"


class InvalidTransition(SessionError):
    code = "invalid_transition"


@dataclass
class _Participant:
    participant_id: str
    client_id: str
    device_name: str
    operating_system: str
    status: ParticipantStatus
    role: ParticipantRole
    joined_at: datetime | None = None
    last_heartbeat: datetime | None = None
    token_digest: bytes | None = None
    permission: PermissionLevel | None = PermissionLevel.READ_ONLY

    def view(self) -> ParticipantView:
        return ParticipantView(
            participant_id=self.participant_id,
            device_name=self.device_name,
            operating_system=self.operating_system,
            status=self.status,
            joined_at=self.joined_at,
            last_heartbeat=self.last_heartbeat,
            role=self.role,
            permission=self.permission,
        )


@dataclass
class _JoinRequest:
    request_id: str
    client_id: str
    device_name: str
    operating_system: str
    requested_at: datetime
    status: JoinRequestStatus
    request_token_digest: bytes
    participant_id: str
    participant_token: str | None = None

    def view(self) -> JoinRequestView:
        return JoinRequestView(
            request_id=self.request_id,
            client_id=self.client_id,
            device_name=self.device_name,
            operating_system=self.operating_system,
            requested_at=self.requested_at,
            status=self.status,
        )


@dataclass(frozen=True)
class SessionCredentials:
    """Secrets returned once to the host when a session is created."""

    joining_code: str
    host_token: str
    host_participant_id: str


class SessionManager:
    """Own one live session, its join requests, participants, and credentials."""

    def __init__(
        self,
        *,
        heartbeat_timeout_seconds: int = 25,
        request_timeout_seconds: int = 180,
        max_join_attempts_per_minute: int = 20,
    ) -> None:
        if heartbeat_timeout_seconds < 5:
            raise ValueError("Heartbeat timeout must be at least five seconds.")
        self.heartbeat_timeout = timedelta(seconds=heartbeat_timeout_seconds)
        self.request_timeout = timedelta(seconds=request_timeout_seconds)
        self.max_join_attempts_per_minute = max_join_attempts_per_minute
        self._lock = threading.RLock()
        self._session: PublicSession | None = None
        self._joining_code_salt: bytes | None = None
        self._joining_code_digest: bytes | None = None
        self._host_token_digest: bytes | None = None
        self._host_participant_id: str | None = None
        self._participants: dict[str, _Participant] = {}
        self._requests: dict[str, _JoinRequest] = {}
        self._request_by_client: dict[str, str] = {}
        self._attempts_by_address: dict[str, deque[datetime]] = defaultdict(deque)

    def create_session(
        self,
        *,
        session_name: str,
        host_device_name: str,
        host_os: str,
        host_ip: str,
        port: int,
        certificate_fingerprint: str | None = None,
        now: datetime | None = None,
    ) -> tuple[PublicSession, SessionCredentials]:
        """Create session state and return the host's one-time credentials."""
        session_name = self._clean_text(session_name, "Session name", 1, 80)
        host_device_name = self._clean_text(host_device_name, "Device name", 1, 80)
        host_os = self._clean_text(host_os, "Operating system", 1, 40)
        host_ip = self._clean_text(host_ip, "Host IP address", 1, 64)
        parsed_host_ip = ipaddress.ip_address(host_ip)
        if (
            parsed_host_ip.version != 4
            or parsed_host_ip.is_global
            or parsed_host_ip.is_loopback
            or parsed_host_ip.is_unspecified
        ):
            raise ValueError("Host address must be a usable local IPv4 address.")
        if not 1 <= port <= 65535:
            raise ValueError("Session port must be between 1 and 65535.")

        current = now or self._now()
        joining_code = "".join(secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH))
        salt = secrets.token_bytes(16)
        code_digest = self._hash_join_code(joining_code, salt)
        host_token = secrets.token_urlsafe(32)
        host_participant_id = uuid.uuid4().hex
        host = PublicSession(
            session_id=uuid.uuid4().hex,
            session_name=session_name,
            host_device_name=host_device_name,
            host_os=host_os,
            host_ip=host_ip,
            port=port,
            participant_count=1,
            status=SessionStatus.ACTIVE,
            created_at=current,
            certificate_fingerprint=certificate_fingerprint,
        )
        participant = _Participant(
            participant_id=host_participant_id,
            client_id=f"host-{host.session_id}",
            device_name=host_device_name,
            operating_system=host_os,
            status=ParticipantStatus.CONNECTED,
            role=ParticipantRole.HOST,
            permission=None,
            joined_at=current,
            last_heartbeat=current,
        )
        with self._lock:
            if self._session is not None and self._session.status is SessionStatus.ACTIVE:
                raise InvalidTransition("This device already has an active session.")
            self._session = host
            self._joining_code_salt = salt
            self._joining_code_digest = code_digest
            self._host_token_digest = self._token_digest(host_token)
            self._host_participant_id = host_participant_id
            self._participants = {host_participant_id: participant}
            self._requests.clear()
            self._request_by_client.clear()
            self._attempts_by_address.clear()
        return host, SessionCredentials(joining_code, host_token, host_participant_id)

    def get_public_session(self) -> PublicSession:
        with self._lock:
            session = self._active_session()
            return session.model_copy(deep=True)

    def host_snapshot(self, host_token: str) -> tuple[PublicSession, list[JoinRequestView], list[ParticipantView]]:
        with self._lock:
            self._require_host(host_token)
            session = self._active_session()
            pending = [
                request.view()
                for request in self._requests.values()
                if request.status is JoinRequestStatus.PENDING
            ]
            participants = [participant.view() for participant in self._participants.values()]
            return session.model_copy(deep=True), pending, participants

    def request_join(
        self,
        *,
        session_id: str,
        client_id: str,
        device_name: str,
        operating_system: str,
        joining_code: str,
        source_address: str,
        now: datetime | None = None,
    ) -> tuple[str, str, JoinRequestStatus]:
        """Validate the code and create one pending request per client identity."""
        client_id = self._clean_text(client_id, "Client ID", 1, 80)
        device_name = self._clean_text(device_name, "Device name", 1, 80)
        operating_system = self._clean_text(operating_system, "Operating system", 1, 40)
        code = "".join(char for char in joining_code.upper() if char not in " -")
        if (
            not 8 <= len(code) <= 32
            or any(char not in JOIN_CODE_ALPHABET for char in code)
        ):
            raise InvalidCredential("The joining code is invalid.")
        current = now or self._now()
        with self._lock:
            session = self._active_session()
            if not secrets.compare_digest(session.session_id, session_id):
                raise SessionUnavailable("The requested session is unavailable.")
            self._check_join_rate(source_address, current)
            if not self._valid_join_code(code):
                raise InvalidCredential("The joining code is invalid.")
            existing_id = self._request_by_client.get(client_id)
            if existing_id:
                existing = self._requests[existing_id]
                request_token = secrets.token_urlsafe(32)
                existing.request_token_digest = self._token_digest(request_token)
                return existing.request_id, request_token, existing.status

            request_token = secrets.token_urlsafe(32)
            request_id = uuid.uuid4().hex
            participant_id = uuid.uuid4().hex
            request = _JoinRequest(
                request_id=request_id,
                client_id=client_id,
                device_name=device_name,
                operating_system=operating_system,
                requested_at=current,
                status=JoinRequestStatus.PENDING,
                request_token_digest=self._token_digest(request_token),
                participant_id=participant_id,
            )
            participant = _Participant(
                participant_id=participant_id,
                client_id=client_id,
                device_name=device_name,
                operating_system=operating_system,
                status=ParticipantStatus.PENDING,
                role=ParticipantRole.PARTICIPANT,
            )
            self._requests[request_id] = request
            self._request_by_client[client_id] = request_id
            self._participants[participant_id] = participant
            self._refresh_count()
            return request_id, request_token, request.status

    def poll_join_request(self, request_id: str, request_token: str) -> tuple[JoinRequestStatus, str | None]:
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or not self._token_matches(request_token, request.request_token_digest):
                raise InvalidCredential("The join request credential is invalid or expired.")
            return request.status, request.participant_token

    def approve_request(self, request_id: str, host_token: str) -> JoinRequestView:
        with self._lock:
            self._require_host(host_token)
            request = self._requests.get(request_id)
            if request is None:
                raise SessionUnavailable("Join request not found.")
            if request.status is JoinRequestStatus.APPROVED:
                return request.view()
            if request.status is not JoinRequestStatus.PENDING:
                raise InvalidTransition("Only a pending request can be approved.")
            participant = self._participants[request.participant_id]
            participant_token = secrets.token_urlsafe(32)
            participant.token_digest = self._token_digest(participant_token)
            request.participant_token = participant_token
            request.status = JoinRequestStatus.APPROVED
            return request.view()

    def reject_request(self, request_id: str, host_token: str) -> JoinRequestView:
        with self._lock:
            self._require_host(host_token)
            request = self._requests.get(request_id)
            if request is None:
                raise SessionUnavailable("Join request not found.")
            if request.status is JoinRequestStatus.REJECTED:
                return request.view()
            if request.status is not JoinRequestStatus.PENDING:
                raise InvalidTransition("Only a pending request can be rejected.")
            request.status = JoinRequestStatus.REJECTED
            participant = self._participants[request.participant_id]
            participant.status = ParticipantStatus.REJECTED
            participant.token_digest = None
            return request.view()

    def authenticate_participant(self, participant_token: str, *, now: datetime | None = None) -> ParticipantView:
        current = now or self._now()
        with self._lock:
            self._active_session()
            participant = self._participant_for_token(participant_token)
            if participant.status not in {ParticipantStatus.PENDING, ParticipantStatus.CONNECTED, ParticipantStatus.DISCONNECTED}:
                raise InvalidCredential("Participant credential is no longer active.")
            participant.status = ParticipantStatus.CONNECTED
            participant.last_heartbeat = current
            if participant.joined_at is None:
                participant.joined_at = current
            self._refresh_count()
            return participant.view()

    def authenticate_http_participant(self, participant_token: str) -> ParticipantView:
        """Authenticate HTTP file requests without promoting a disconnected peer."""
        with self._lock:
            self._active_session()
            participant = self._participant_for_token(participant_token)
            if (
                participant.role is not ParticipantRole.PARTICIPANT
                or participant.status is not ParticipantStatus.CONNECTED
            ):
                raise InvalidCredential("Participant is not connected.")
            return participant.view()

    def require_participant_permission(
        self,
        participant_token: str,
        required: PermissionLevel,
    ) -> ParticipantView:
        with self._lock:
            self._active_session()
            participant = self._participant_for_token(participant_token)
            if (
                participant.role is not ParticipantRole.PARTICIPANT
                or participant.status is not ParticipantStatus.CONNECTED
            ):
                raise InvalidCredential("Participant is not connected.")
            if required is PermissionLevel.READ_WRITE and participant.permission is not PermissionLevel.READ_WRITE:
                raise PermissionDenied("This participant does not have upload permission.")
            return participant.view()

    def set_participant_permission(
        self,
        participant_id: str,
        permission: PermissionLevel,
        host_token: str,
    ) -> ParticipantView:
        """Change a participant's server-owned access level using host credentials."""
        if not isinstance(permission, PermissionLevel):
            try:
                permission = PermissionLevel(permission)
            except (TypeError, ValueError) as error:
                raise ValueError("Permission must be READ_ONLY or READ_WRITE.") from error
        with self._lock:
            self._require_host(host_token)
            participant = self._participants.get(participant_id)
            if (
                participant is None
                or participant.role is not ParticipantRole.PARTICIPANT
                or participant.status not in {ParticipantStatus.CONNECTED, ParticipantStatus.DISCONNECTED}
            ):
                raise SessionUnavailable("An approved participant was not found.")
            participant.permission = permission
            return participant.view()

    def connected_participant_ids(self) -> set[str]:
        """Return connected ordinary participant IDs for transfer cleanup."""
        with self._lock:
            return {
                item.participant_id
                for item in self._participants.values()
                if item.role is ParticipantRole.PARTICIPANT
                and item.status is ParticipantStatus.CONNECTED
            }

    def heartbeat(self, participant_token: str, *, now: datetime | None = None) -> ParticipantView:
        current = now or self._now()
        with self._lock:
            participant = self._participant_for_token(participant_token)
            if participant.status is not ParticipantStatus.CONNECTED:
                raise InvalidCredential("Participant is not connected.")
            participant.last_heartbeat = current
            return participant.view()

    def disconnect_participant(self, participant_id: str, host_token: str) -> ParticipantView:
        with self._lock:
            self._require_host(host_token)
            participant = self._participants.get(participant_id)
            if participant is None or participant.role is ParticipantRole.HOST:
                raise SessionUnavailable("Participant not found.")
            if participant.status is not ParticipantStatus.CONNECTED:
                raise InvalidTransition("Only a connected participant can be disconnected.")
            participant.status = ParticipantStatus.DISCONNECTED
            participant.token_digest = None
            request_id = self._request_by_client.get(participant.client_id)
            if request_id:
                self._requests[request_id].participant_token = None
            self._refresh_count()
            return participant.view()

    def participant_disconnected(self, participant_id: str) -> ParticipantView | None:
        """Mark a WebSocket departure without revoking a temporary reconnect."""
        with self._lock:
            participant = self._participants.get(participant_id)
            if participant is None or participant.role is ParticipantRole.HOST:
                return None
            if participant.status is ParticipantStatus.CONNECTED:
                participant.status = ParticipantStatus.DISCONNECTED
                self._refresh_count()
            return participant.view()

    def expire_inactive(self, *, now: datetime | None = None) -> list[str]:
        """Expire join requests and stale connected peers; return event names."""
        current = now or self._now()
        changes: list[str] = []
        with self._lock:
            if self._session is None or self._session.status is not SessionStatus.ACTIVE:
                return changes
            for request in self._requests.values():
                if (
                    request.status is JoinRequestStatus.PENDING
                    and current - request.requested_at > self.request_timeout
                ):
                    request.status = JoinRequestStatus.EXPIRED
                    participant = self._participants[request.participant_id]
                    participant.status = ParticipantStatus.REJECTED
                    participant.token_digest = None
                    changes.append("join_request_expired")
            for participant in self._participants.values():
                if (
                    participant.role is ParticipantRole.PARTICIPANT
                    and participant.status is ParticipantStatus.CONNECTED
                    and participant.last_heartbeat is not None
                    and current - participant.last_heartbeat > self.heartbeat_timeout
                ):
                    participant.status = ParticipantStatus.DISCONNECTED
                    participant.token_digest = None
                    request_id = self._request_by_client.get(participant.client_id)
                    if request_id:
                        self._requests[request_id].participant_token = None
                    changes.append("participant_disconnected")
            self._refresh_count()
        return changes

    def end_session(self, host_token: str) -> PublicSession:
        with self._lock:
            self._require_host(host_token)
            session = self._active_session()
            ended = session.model_copy(update={"status": SessionStatus.ENDED, "participant_count": 0})
            self._session = ended
            for participant in self._participants.values():
                if participant.status in {ParticipantStatus.CONNECTED, ParticipantStatus.PENDING}:
                    participant.status = ParticipantStatus.DISCONNECTED
                participant.token_digest = None
            for request in self._requests.values():
                request.participant_token = None
            self._joining_code_digest = None
            self._host_token_digest = None
            return ended.model_copy(deep=True)

    def host_token_valid(self, token: str) -> bool:
        with self._lock:
            return self._host_token_digest is not None and self._token_matches(token, self._host_token_digest)

    def active_session(self) -> bool:
        with self._lock:
            return self._session is not None and self._session.status is SessionStatus.ACTIVE

    def _active_session(self) -> PublicSession:
        if self._session is None or self._session.status is not SessionStatus.ACTIVE:
            raise SessionUnavailable("There is no active ShareSpace session.")
        return self._session

    def _require_host(self, token: str) -> None:
        if self._host_token_digest is None or not self._token_matches(token, self._host_token_digest):
            raise PermissionDenied("Host credentials are invalid or expired.")
        self._active_session()

    def _participant_for_token(self, token: str) -> _Participant:
        token_digest = self._token_digest(token)
        for participant in self._participants.values():
            if participant.token_digest is not None and hmac.compare_digest(participant.token_digest, token_digest):
                return participant
        raise InvalidCredential("Participant credentials are invalid or expired.")

    def _valid_join_code(self, code: str) -> bool:
        if self._joining_code_digest is None or self._joining_code_salt is None:
            return False
        digest = self._hash_join_code(code, self._joining_code_salt)
        return hmac.compare_digest(digest, self._joining_code_digest)

    def _check_join_rate(self, source_address: str, now: datetime) -> None:
        attempts = self._attempts_by_address[source_address]
        cutoff = now - timedelta(minutes=1)
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        if len(attempts) >= self.max_join_attempts_per_minute:
            raise InvalidCredential("Too many join attempts. Wait a minute and try again.")
        attempts.append(now)

    def _refresh_count(self) -> None:
        if self._session is not None and self._session.status is SessionStatus.ACTIVE:
            count = sum(
                participant.status is ParticipantStatus.CONNECTED
                for participant in self._participants.values()
            )
            self._session = self._session.model_copy(update={"participant_count": count})

    @staticmethod
    def _clean_text(value: str, label: str, minimum: int, maximum: int) -> str:
        cleaned = value.strip()
        if not minimum <= len(cleaned) <= maximum or any(
            not char.isprintable() for char in cleaned
        ):
            raise ValueError(f"{label} must contain {minimum} to {maximum} printable characters.")
        return cleaned

    @staticmethod
    def _token_digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()

    @staticmethod
    def _token_matches(token: str, expected_digest: bytes) -> bool:
        return hmac.compare_digest(SessionManager._token_digest(token), expected_digest)

    @staticmethod
    def _hash_join_code(code: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", code.encode("ascii"), salt, JOIN_CODE_PBKDF2_ROUNDS
        )

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)
