"""Authenticated HTTP routes for browsing and transferring shared files."""

from __future__ import annotations

import base64
import binascii
import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from sharespace.core.file_manager import FileAccessError, MAX_FILE_SIZE_BYTES
from sharespace.core.models import (
    PermissionLevel,
    SharedDirectoryState,
    SharedDirectoryUpdate,
    TransferView,
    UploadSettingUpdate,
)
from sharespace.core.session_manager import InvalidCredential, PermissionDenied, SessionManager
from sharespace.core.transfer_manager import TransferCancelled, TransferNotFound
from sharespace.networking.file_service import FileService, content_disposition, guess_content_type


def create_file_router(
    manager: SessionManager, file_service: FileService, *, event_broker=None
) -> APIRouter:
    router = APIRouter()

    def token_value(authorization: str | None, *, host: bool = False) -> str:
        error = PermissionDenied if host else InvalidCredential
        if not authorization:
            raise error("Authorization is required.")
        scheme, _, value = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not value.strip():
            raise error("Authorization is invalid.")
        return value.strip()

    def participant(authorization: str | None):
        return manager.authenticate_http_participant(token_value(authorization))

    def host(authorization: str | None) -> str:
        token = token_value(authorization, host=True)
        if not manager.host_token_valid(token):
            raise PermissionDenied("Host credentials are invalid or expired.")
        return token

    async def publish_file_state() -> SharedDirectoryState:
        state = file_service.state()
        if event_broker is not None:
            await event_broker.publish({
                "type": "file_sharing_state_changed",
                "available": state.available,
                "uploads_enabled": state.uploads_enabled,
                "directory_name": state.directory_name,
            })
        return state

    @router.get("/api/v1/files/settings", response_model=SharedDirectoryState)
    async def file_settings(authorization: str | None = Header(default=None)) -> SharedDirectoryState:
        peer = participant(authorization)
        return file_service.state().model_copy(update={"permission": peer.permission})

    @router.get("/api/v1/files", response_model=None)
    async def browse_files(
        path: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        participant(authorization)
        return file_service.browse(path)

    @router.get("/api/v1/files/info")
    async def file_info(
        path: str = Query(...),
        authorization: str | None = Header(default=None),
    ):
        participant(authorization)
        listing = file_service.directories.listing(path.rpartition("/")[0] if "/" in path else "")
        for item in listing.items:
            if item.relative_path == path:
                return item
        raise FileAccessError("The requested item was not found.", "file_not_found", 404)

    @router.get("/api/v1/files/download")
    async def download_file(
        path: str = Query(...),
        transfer_id: str | None = Header(default=None, alias="X-Transfer-ID"),
        authorization: str | None = Header(default=None),
    ):
        peer = participant(authorization)
        identifier = _valid_transfer_id(transfer_id)
        file_path, descriptor, record = file_service.start_download(path, peer.participant_id, identifier)
        headers = {
            "Content-Length": str(record.total_bytes),
            "Content-Disposition": content_disposition(file_path.name),
            "X-Transfer-ID": identifier,
            "X-Content-Type-Options": "nosniff",
        }
        return StreamingResponse(
            file_service.download_chunks(descriptor, identifier),
            media_type=guess_content_type(file_path.name),
            headers=headers,
        )

    @router.post("/api/v1/files/upload")
    async def upload_file(
        request: Request,
        path: str = Query(default=""),
        filename_header: str | None = Header(default=None, alias="X-Filename"),
        size: int | None = Header(default=None, alias="X-File-Size"),
        transfer_id: str | None = Header(default=None, alias="X-Transfer-ID"),
        authorization: str | None = Header(default=None),
    ):
        participant_token = token_value(authorization)
        peer = manager.require_participant_permission(participant_token, PermissionLevel.READ_WRITE)
        if not file_service.uploads_enabled:
            raise FileAccessError("The host has not enabled uploads.", "uploads_disabled", 403)
        if filename_header is None:
            raise FileAccessError("The upload filename is required.", "invalid_filename", 400)
        if len(filename_header) > 340 or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
            for character in filename_header
        ):
            raise FileAccessError("The upload filename is invalid.", "invalid_filename", 400)
        try:
            padded = filename_header + "=" * (-len(filename_header) % 4)
            filename = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            raise FileAccessError("The upload filename is invalid.", "invalid_filename", 400) from error
        if size is None:
            raise FileAccessError("The upload size is required.", "invalid_size", 400)
        if size < 0 or size > MAX_FILE_SIZE_BYTES:
            raise FileAccessError("Uploads are limited to 20 MB per file.", "file_too_large", 413)
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) != size:
                    raise FileAccessError("Content length does not match the declared file size.", "size_mismatch", 400)
            except ValueError as error:
                if isinstance(error, FileAccessError):
                    raise
                raise FileAccessError("Content length is invalid.", "invalid_size", 400) from error
        identifier = _valid_transfer_id(transfer_id)
        iterator = request.stream().__aiter__()

        async def next_chunk() -> bytes:
            try:
                return await iterator.__anext__()
            except StopAsyncIteration:
                return b""

        return await file_service.receive_upload(
            relative_directory=path,
            filename=filename,
            expected_size=size,
            transfer_id=identifier,
            participant_id=peer.participant_id,
            body=next_chunk,
            authorize_upload=lambda: manager.require_participant_permission(
                participant_token, PermissionLevel.READ_WRITE
            ),
        )

    @router.get("/api/v1/files/transfers", response_model=list[TransferView])
    async def participant_transfers(authorization: str | None = Header(default=None)):
        peer = participant(authorization)
        return file_service.transfers.list(participant_id=peer.participant_id)

    @router.post("/api/v1/files/transfers/{transfer_id}/cancel", response_model=TransferView)
    async def cancel_participant_transfer(
        transfer_id: str,
        authorization: str | None = Header(default=None),
    ):
        peer = participant(authorization)
        identifier = _valid_transfer_id(transfer_id)
        return await file_service.cancel_transfer(identifier, participant_id=peer.participant_id)

    @router.get("/api/v1/host/files/state", response_model=SharedDirectoryState)
    async def host_file_state(authorization: str | None = Header(default=None)):
        host(authorization)
        return file_service.state()

    @router.post("/api/v1/host/files/uploads", response_model=SharedDirectoryState)
    async def host_upload_setting(
        data: UploadSettingUpdate,
        authorization: str | None = Header(default=None),
    ):
        host(authorization)
        if data.enabled and file_service.directories.root is None:
            raise FileAccessError("Select a shared directory before enabling uploads.", "sharing_unavailable", 409)
        await file_service.set_uploads_enabled_guarded(data.enabled)
        return await publish_file_state()

    @router.post("/api/v1/host/files/root", response_model=SharedDirectoryState)
    async def host_select_shared_root(
        data: SharedDirectoryUpdate,
        authorization: str | None = Header(default=None),
    ):
        host(authorization)
        await file_service.select_root_guarded(data.path)
        return await publish_file_state()

    @router.post("/api/v1/host/files/stop", response_model=SharedDirectoryState)
    async def host_stop_sharing(authorization: str | None = Header(default=None)):
        host(authorization)
        await file_service.stop_sharing_guarded()
        return await publish_file_state()

    @router.get("/api/v1/host/files/transfers", response_model=list[TransferView])
    async def host_transfers(authorization: str | None = Header(default=None)):
        host(authorization)
        return file_service.transfers.list()

    @router.post("/api/v1/host/files/transfers/{transfer_id}/cancel", response_model=TransferView)
    async def host_cancel_transfer(transfer_id: str, authorization: str | None = Header(default=None)):
        host(authorization)
        return await file_service.cancel_transfer(_valid_transfer_id(transfer_id))

    return router


def _valid_transfer_id(value: str | None) -> str:
    if value is None:
        return uuid.uuid4().hex
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
        raise FileAccessError("Transfer ID is invalid.", "invalid_transfer_id", 400)
    return value
