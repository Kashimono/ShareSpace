"""Validated data models shared by the session API and desktop UI."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

import ipaddress

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionStatus(StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


class ParticipantRole(StrEnum):
    HOST = "HOST"
    PARTICIPANT = "PARTICIPANT"


class PermissionLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    READ_WRITE = "READ_WRITE"


class ParticipantStatus(StrEnum):
    PENDING = "PENDING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    REJECTED = "REJECTED"


class JoinRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class FileItemType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"


class TransferDirection(StrEnum):
    UPLOAD = "UPLOAD"
    DOWNLOAD = "DOWNLOAD"


class TransferStatus(StrEnum):
    QUEUED = "QUEUED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class PublicSession(BaseModel):
    """Session data safe to expose to nearby devices."""

    model_config = ConfigDict(frozen=True)

    session_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_name: str = Field(min_length=1, max_length=80)
    host_device_name: str = Field(min_length=1, max_length=80)
    host_os: str = Field(min_length=1, max_length=40)
    host_ip: str
    port: int = Field(ge=1, le=65535)
    participant_count: int = Field(ge=1)
    status: SessionStatus
    created_at: datetime
    certificate_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("session_name", "host_device_name", "host_os")
    @classmethod
    def validate_display_text(cls, value: str) -> str:
        if not value.strip() or any(not character.isprintable() for character in value):
            raise ValueError("Display text must contain printable characters.")
        return value

    @field_validator("host_ip")
    @classmethod
    def validate_local_ipv4(cls, value: str) -> str:
        address = ipaddress.ip_address(value)
        if (
            address.version != 4
            or address.is_global
            or address.is_loopback
            or address.is_unspecified
        ):
            raise ValueError("Session host address must be a local IPv4 address.")
        return str(address)

    @field_validator("certificate_fingerprint", mode="before")
    @classmethod
    def normalize_certificate_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return "".join(character for character in value.lower() if character in "0123456789abcdef")


class JoinRequestCreate(BaseModel):
    """Information a device submits when asking to join a session."""

    client_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    device_name: str = Field(min_length=1, max_length=80)
    operating_system: str = Field(min_length=1, max_length=40)
    joining_code: str = Field(min_length=8, max_length=32)

    @field_validator("device_name", "operating_system")
    @classmethod
    def normalize_display_text(cls, value: str) -> str:
        value = value.strip()
        if not value or any(not character.isprintable() for character in value):
            raise ValueError("Display text must contain printable characters.")
        return value

    @field_validator("joining_code")
    @classmethod
    def validate_joining_code_text(cls, value: str) -> str:
        if not value.isascii():
            raise ValueError("Joining code must use ASCII characters.")
        return value.strip()


class JoinRequestCreated(BaseModel):
    request_id: str
    request_token: str
    status: JoinRequestStatus


class JoinRequestView(BaseModel):
    request_id: str
    client_id: str
    device_name: str
    operating_system: str
    requested_at: datetime
    status: JoinRequestStatus


class JoinRequestPoll(BaseModel):
    request_id: str
    status: JoinRequestStatus
    participant_token: str | None = None
    detail: str | None = None


class ParticipantView(BaseModel):
    participant_id: str
    device_name: str
    operating_system: str
    status: ParticipantStatus
    joined_at: datetime | None = None
    last_heartbeat: datetime | None = None
    role: ParticipantRole
    permission: PermissionLevel | None = None


class HostSnapshot(BaseModel):
    session: PublicSession
    pending_requests: list[JoinRequestView]
    participants: list[ParticipantView]


class ApiError(BaseModel):
    detail: str
    code: str


class FileItem(BaseModel):
    filename: str
    relative_path: str
    extension: str
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    item_type: FileItemType
    accessible: bool = True
    unavailable_reason: str | None = None


class DirectoryListing(BaseModel):
    directory_path: str
    parent_path: str | None
    shared_directory_name: str
    items: list[FileItem]


class SharedDirectoryState(BaseModel):
    shared: bool
    available: bool
    directory_name: str | None = None
    uploads_enabled: bool = False
    permission: PermissionLevel | None = None


class UploadSettingUpdate(BaseModel):
    enabled: bool


class ParticipantPermissionUpdate(BaseModel):
    permission: PermissionLevel


class SharedDirectoryUpdate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class TransferView(BaseModel):
    transfer_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    filename: str
    direction: TransferDirection
    total_bytes: int = Field(ge=0)
    transferred_bytes: int = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)
    status: TransferStatus
    started_at: datetime
    completed_at: datetime | None = None
    error: str | None = None
    participant_id: str
