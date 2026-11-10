"""Validated, credential-free events carried by the authenticated WSS channel."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sharespace.core.models import PermissionLevel


class SessionEventType(StrEnum):
    FILE_CREATED = "FILE_CREATED"
    FILE_MODIFIED = "FILE_MODIFIED"
    FILE_DELETED = "FILE_DELETED"
    PARTICIPANT_JOINED = "PARTICIPANT_JOINED"
    PARTICIPANT_LEFT = "PARTICIPANT_LEFT"
    PARTICIPANT_PERMISSION_CHANGED = "PARTICIPANT_PERMISSION_CHANGED"
    SHARED_DIRECTORY_CHANGED = "SHARED_DIRECTORY_CHANGED"
    SHARED_DIRECTORY_UNAVAILABLE = "SHARED_DIRECTORY_UNAVAILABLE"
    FILE_SHARING_STATE_CHANGED = "FILE_SHARING_STATE_CHANGED"
    JOIN_REQUEST_PENDING = "JOIN_REQUEST_PENDING"
    JOIN_REQUEST_APPROVED = "JOIN_REQUEST_APPROVED"
    JOIN_REQUEST_REJECTED = "JOIN_REQUEST_REJECTED"
    JOIN_REQUEST_EXPIRED = "JOIN_REQUEST_EXPIRED"
    SESSION_ENDED = "SESSION_ENDED"
    SESSION_RESYNC_REQUIRED = "SESSION_RESYNC_REQUIRED"


class FileEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = Field(min_length=1, max_length=255)
    relative_path: str = Field(min_length=1, max_length=4096)
    parent_path: str = Field(default="", max_length=4096)
    size_bytes: int | None = Field(default=None, ge=0)
    modified_at: datetime | None = None

    @model_validator(mode="after")
    def validate_relative_identity(self) -> FileEventData:
        path = self.relative_path
        if (
            path.startswith("/")
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or path.rsplit("/", 1)[-1] != self.filename
            or path.rpartition("/")[0] != self.parent_path
            or any(part.startswith(".") for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
            or self.filename.casefold().endswith((".part", ".partial", ".tmp", ".temp"))
            or self.filename.startswith("~$")
        ):
            raise ValueError("File events require a safe public relative path.")
        return self


class ParticipantActivityData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    device_name: str = Field(min_length=1, max_length=80)
    operating_system: str = Field(min_length=1, max_length=40)
    participant_count: int = Field(ge=1)


class PermissionEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participant_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    permission: PermissionLevel


class DirectoryEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    directory_name: str | None = Field(default=None, max_length=255)
    relative_path: str | None = Field(default=None, max_length=4096)
    change: Literal["created", "deleted"] | None = None

    @model_validator(mode="after")
    def validate_relative_path(self) -> DirectoryEventData:
        path = self.relative_path
        if path is not None and (
            path.startswith("/")
            or "\\" in path
            or ":" in path
            or "\x00" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(part.startswith(".") for part in path.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise ValueError("Directory events require a safe relative path.")
        return self


class FileSharingStateData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    available: bool
    uploads_enabled: bool
    directory_name: str | None = Field(default=None, max_length=255)


class JoinRequestEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class SessionEndedData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class EmptyEventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_DATA_MODELS: dict[SessionEventType, type[BaseModel]] = {
    SessionEventType.FILE_CREATED: FileEventData,
    SessionEventType.FILE_MODIFIED: FileEventData,
    SessionEventType.FILE_DELETED: FileEventData,
    SessionEventType.PARTICIPANT_JOINED: ParticipantActivityData,
    SessionEventType.PARTICIPANT_LEFT: ParticipantActivityData,
    SessionEventType.PARTICIPANT_PERMISSION_CHANGED: PermissionEventData,
    SessionEventType.SHARED_DIRECTORY_CHANGED: DirectoryEventData,
    SessionEventType.SHARED_DIRECTORY_UNAVAILABLE: DirectoryEventData,
    SessionEventType.FILE_SHARING_STATE_CHANGED: FileSharingStateData,
    SessionEventType.JOIN_REQUEST_PENDING: JoinRequestEventData,
    SessionEventType.JOIN_REQUEST_APPROVED: JoinRequestEventData,
    SessionEventType.JOIN_REQUEST_REJECTED: JoinRequestEventData,
    SessionEventType.JOIN_REQUEST_EXPIRED: JoinRequestEventData,
    SessionEventType.SESSION_ENDED: SessionEndedData,
    SessionEventType.SESSION_RESYNC_REQUIRED: EmptyEventData,
}

_LEGACY_TYPES: dict[SessionEventType, str] = {
    SessionEventType.FILE_CREATED: "file_created",
    SessionEventType.FILE_MODIFIED: "file_modified",
    SessionEventType.FILE_DELETED: "file_deleted",
    SessionEventType.PARTICIPANT_JOINED: "participant_connected",
    SessionEventType.PARTICIPANT_LEFT: "participant_disconnected",
    SessionEventType.PARTICIPANT_PERMISSION_CHANGED: "participant_permission_changed",
    SessionEventType.SHARED_DIRECTORY_CHANGED: "shared_directory_changed",
    SessionEventType.SHARED_DIRECTORY_UNAVAILABLE: "shared_directory_unavailable",
    SessionEventType.FILE_SHARING_STATE_CHANGED: "file_sharing_state_changed",
    SessionEventType.JOIN_REQUEST_PENDING: "join_request_pending",
    SessionEventType.JOIN_REQUEST_APPROVED: "join_request_approved",
    SessionEventType.JOIN_REQUEST_REJECTED: "join_request_rejected",
    SessionEventType.JOIN_REQUEST_EXPIRED: "join_request_expired",
    SessionEventType.SESSION_ENDED: "session_ended",
    SessionEventType.SESSION_RESYNC_REQUIRED: "session_resync_required",
}
_EVENT_TYPES_BY_LEGACY = {value: key for key, value in _LEGACY_TYPES.items()}
logger = logging.getLogger(__name__)


class RecentEventIds:
    """Bounded in-memory duplicate filter shared across one reconnect loop."""

    def __init__(self, capacity: int = 2048) -> None:
        if capacity < 1:
            raise ValueError("Event ID capacity must be positive.")
        self._ids: deque[str] = deque(maxlen=capacity)
        self._set: set[str] = set()

    def add(self, event_id: str) -> bool:
        """Return True for a new identifier, False when it was seen recently."""
        if event_id in self._set:
            return False
        if len(self._ids) == self._ids.maxlen:
            self._set.discard(self._ids[0])
        self._ids.append(event_id)
        self._set.add(event_id)
        return True


class SessionEvent(BaseModel):
    """A safe session event with a stable ID for client-side duplicate suppression."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex, pattern=r"^[0-9a-f]{32}$")
    event_type: SessionEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any]

    @model_validator(mode="after")
    def validate_event_data(self) -> SessionEvent:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("Session event timestamps must include a timezone.")
        model = _DATA_MODELS[self.event_type]
        payload = model.model_validate(self.data)
        object.__setattr__(self, "data", payload.model_dump(mode="json"))
        return self

    @property
    def legacy_type(self) -> str:
        return _LEGACY_TYPES[self.event_type]

    def to_message(self) -> dict[str, Any]:
        """Return the v0.6 envelope and legacy flat fields used by existing UI code."""
        return {
            "type": self.legacy_type,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "data": dict(self.data),
            **self.data,
        }


def normalize_legacy_event(event: dict[str, Any]) -> SessionEvent:
    """Validate an existing broker event and wrap it in the v0.6 event envelope."""
    legacy_type = event.get("type")
    try:
        event_type = _EVENT_TYPES_BY_LEGACY[legacy_type]
    except (KeyError, TypeError) as error:
        raise ValueError("Unsupported session event type.") from error
    raw_data = event.get("data")
    if raw_data is None:
        raw_data = {
            key: value
            for key, value in event.items()
            if key not in {"type", "event_id", "event_type", "timestamp"}
        }
    return SessionEvent(
        event_id=event.get("event_id", uuid.uuid4().hex),
        event_type=event_type,
        timestamp=event.get("timestamp", datetime.now(timezone.utc)),
        data=raw_data,
    )


def validate_wire_event(message: dict[str, Any]) -> SessionEvent:
    """Parse an incoming broker event and verify its legacy alias and flat fields."""
    event = SessionEvent.model_validate({
        "event_id": message.get("event_id"),
        "event_type": message.get("event_type"),
        "timestamp": message.get("timestamp"),
        "data": message.get("data"),
    })
    if message.get("type") != event.legacy_type:
        raise ValueError("Session event type alias is invalid.")
    for key, value in event.data.items():
        if message.get(key) != value:
            raise ValueError("Session event fields do not match its validated payload.")
    return event


class EventBroker:
    """Fan validated session events out without letting one slow peer block others."""

    def __init__(self, *, queue_size: int = 1000) -> None:
        if queue_size < 1:
            raise ValueError("Event queue size must be positive.")
        self._queues: dict[asyncio.Queue[dict[str, Any]], str | None] = {}
        self._active_participants: set[str] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._queues[queue] = None
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.pop(queue, None)

    def bind_participant(self, queue: asyncio.Queue[dict[str, Any]], participant_id: str) -> None:
        if queue in self._queues:
            self._queues[queue] = participant_id

    def claim_participant(self, participant_id: str) -> bool:
        """Allow only one live WSS connection for one participant identity."""
        if participant_id in self._active_participants:
            return False
        self._active_participants.add(participant_id)
        return True

    def release_participant(self, participant_id: str) -> None:
        self._active_participants.discard(participant_id)

    async def publish(
        self,
        event: dict[str, Any] | SessionEvent,
        *,
        target_participant_id: str | None = None,
        exclude_participant_id: str | None = None,
    ) -> None:
        """Validate once, then enqueue independently for each eligible connection."""
        try:
            normalized = event if isinstance(event, SessionEvent) else normalize_legacy_event(event)
            message = normalized.to_message()
        except (TypeError, ValueError) as error:
            logger.warning("Rejected invalid outgoing session event (%s).", type(error).__name__)
            return

        for queue, participant_id in tuple(self._queues.items()):
            if target_participant_id is not None and participant_id != target_participant_id:
                continue
            if exclude_participant_id is not None and participant_id == exclude_participant_id:
                continue
            if queue.full():
                while not queue.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                recovery = SessionEvent(
                    event_type=SessionEventType.SESSION_RESYNC_REQUIRED,
                    data={},
                ).to_message()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(recovery)
                if queue.full():
                    continue
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(message)
