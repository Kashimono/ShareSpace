import pytest

from sharespace.core.models import TransferDirection, TransferStatus
from sharespace.core.transfer_manager import TransferCancelled, TransferManager, TransferNotFound


def test_progress_and_completion_require_exact_byte_count():
    manager = TransferManager()
    created = manager.create(
        filename="photo.png", direction=TransferDirection.DOWNLOAD,
        total=5, participant_id="peer-a",
    )
    manager.begin(created.transfer_id)
    manager.advance(created.transfer_id, 2)
    assert manager.get(created.transfer_id).progress_percent == 40
    with pytest.raises(ValueError):
        manager.complete(created.transfer_id)
    manager.advance(created.transfer_id, 3)
    assert manager.get(created.transfer_id).progress_percent < 100
    completed = manager.complete(created.transfer_id)
    assert completed.status is TransferStatus.COMPLETED
    assert completed.progress_percent == 100


def test_cancel_sets_cancellation_event_and_stops_progress():
    manager = TransferManager()
    item = manager.create(
        filename="archive.zip", direction=TransferDirection.UPLOAD,
        total=8, participant_id="peer-a",
    )
    event = manager.cancellation_event(item.transfer_id)
    cancelled = manager.cancel(item.transfer_id, participant_id="peer-a")
    assert cancelled.status is TransferStatus.CANCELLED
    assert event.is_set()
    with pytest.raises(TransferCancelled):
        manager.advance(item.transfer_id, 1)


def test_participant_cannot_cancel_another_participants_transfer():
    manager = TransferManager()
    item = manager.create(
        filename="notes.txt", direction=TransferDirection.UPLOAD,
        total=1, participant_id="peer-a",
    )
    with pytest.raises(TransferNotFound):
        manager.cancel(item.transfer_id, participant_id="peer-b")
