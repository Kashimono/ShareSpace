from __future__ import annotations

import asyncio
import uuid

import pytest

from sharespace.core.models import TransferDirection, TransferStatus
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
        ))
        await asyncio.wait_for(body_started.wait(), timeout=2)
        service.transfers.cancel(transfer_id)
        with pytest.raises(TransferCancelled):
            await asyncio.wait_for(transfer_task, timeout=2)

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []
    assert service.transfers.get(transfer_id).status is TransferStatus.CANCELLED
