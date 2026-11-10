from __future__ import annotations

import asyncio
import uuid

import pytest

from sharespace.core.models import PermissionLevel, TransferDirection, TransferStatus
from sharespace.core.session_manager import SessionManager
from sharespace.networking.file_service import FileService
from sharespace.core.transfer_manager import TransferCancelled


def test_interrupted_upload_removes_temporary_file_and_never_publishes_partial_file(tmp_path):
    service = FileService()
    service.select_root(tmp_path)
    service.set_uploads_enabled(True)
    transfer_id = uuid.uuid4().hex
    sequence = iter([b"partial", RuntimeError("connection interrupted")])

    async def next_chunk():
        chunk = next(sequence)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    with pytest.raises(RuntimeError, match="connection interrupted"):
        asyncio.run(service.receive_upload(
            relative_directory="",
            filename="incomplete.bin",
            expected_size=20,
            transfer_id=transfer_id,
            participant_id="peer-1",
            body=next_chunk,
            authorize_upload=lambda: None,
        ))

    assert list(tmp_path.iterdir()) == []
    transfer = service.transfers.get(transfer_id)
    assert transfer.direction is TransferDirection.UPLOAD
    assert transfer.status is TransferStatus.FAILED


def test_upload_cancellation_interrupts_a_stalled_request_body(tmp_path):
    service = FileService()
    service.select_root(tmp_path)
    service.set_uploads_enabled(True)
    transfer_id = uuid.uuid4().hex

    async def run():
        body_started = asyncio.Event()

        async def stalled_body():
            body_started.set()
            await asyncio.Event().wait()

        transfer_task = asyncio.create_task(service.receive_upload(
            relative_directory="",
            filename="stalled.bin",
            expected_size=10,
            transfer_id=transfer_id,
            participant_id="peer-1",
            body=stalled_body,
            authorize_upload=lambda: None,
        ))
        await asyncio.wait_for(body_started.wait(), timeout=2)
        service.transfers.cancel(transfer_id)
        with pytest.raises(TransferCancelled):
            await asyncio.wait_for(transfer_task, timeout=2)

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
    assert service.transfers.get(transfer_id).status is TransferStatus.CANCELLED


def test_revoking_read_write_cancels_active_upload_and_removes_temporary_file(tmp_path):
    manager = SessionManager()
    session, credentials = manager.create_session(
        session_name="Permissions",
        host_device_name="Host",
        host_os="Linux",
        host_ip="192.168.1.20",
        port=54832,
    )
    request_id, request_token, _ = manager.request_join(
        session_id=session.session_id,
        client_id="peer-1",
        device_name="Peer",
        operating_system="Windows",
        joining_code=credentials.joining_code,
        source_address="192.168.1.21",
    )
    manager.approve_request(request_id, credentials.host_token)
    _, participant_token = manager.poll_join_request(request_id, request_token)
    peer = manager.authenticate_participant(participant_token)
    manager.set_participant_permission(
        peer.participant_id, PermissionLevel.READ_WRITE, credentials.host_token
    )

    service = FileService()
    service.select_root(tmp_path)
    service.set_uploads_enabled(True)
    transfer_id = uuid.uuid4().hex

    async def run():
        second_chunk_started = asyncio.Event()
        chunks = iter([b"first", None])

        async def body():
            chunk = next(chunks)
            if chunk is None:
                second_chunk_started.set()
                await asyncio.Event().wait()
            return chunk

        transfer_task = asyncio.create_task(service.receive_upload(
            relative_directory="",
            filename="revoked.bin",
            expected_size=12,
            transfer_id=transfer_id,
            participant_id=peer.participant_id,
            body=body,
            authorize_upload=lambda: manager.require_participant_permission(
                participant_token, PermissionLevel.READ_WRITE
            ),
        ))
        await asyncio.wait_for(second_chunk_started.wait(), timeout=2)
        assert service.transfers.get(transfer_id).transferred_bytes == 5
        async with service.authorization_gate:
            manager.set_participant_permission(
                peer.participant_id, PermissionLevel.READ_ONLY, credentials.host_token
            )
            service.transfers.cancel_active(
                direction=TransferDirection.UPLOAD,
                participant_id=peer.participant_id,
            )
        with pytest.raises(TransferCancelled):
            await asyncio.wait_for(transfer_task, timeout=2)

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
    transfer = service.transfers.get(transfer_id)
    assert transfer.status is TransferStatus.CANCELLED
