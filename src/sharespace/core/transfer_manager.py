"""Thread-safe, in-memory transfer status and cancellation state."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from sharespace.core.models import TransferDirection, TransferStatus, TransferView


class TransferNotFound(LookupError):
    """The requested transfer does not exist or belongs to another peer."""


class TransferCancelled(RuntimeError):
    """Raised when an active transfer was cancelled."""


class _Transfer:
    def __init__(self, *, filename: str, direction: TransferDirection, total: int,
                 participant_id: str, transfer_id: str | None = None) -> None:
        self.transfer_id = transfer_id or uuid.uuid4().hex
        self.filename = filename
        self.direction = direction
        self.total = total
        self.transferred = 0
        self.participant_id = participant_id
        self.status = TransferStatus.QUEUED
        self.started_at = datetime.now(timezone.utc)
        self.completed_at: datetime | None = None
        self.error: str | None = None
        self.cancel_event = threading.Event()

    def view(self) -> TransferView:
        percent = 100.0 if self.status is TransferStatus.COMPLETED else (
            min(100.0, self.transferred * 100.0 / self.total) if self.total else 0.0
        )
        return TransferView(
            transfer_id=self.transfer_id, filename=self.filename, direction=self.direction,
            total_bytes=self.total, transferred_bytes=self.transferred,
            progress_percent=percent, status=self.status, started_at=self.started_at,
            completed_at=self.completed_at, error=self.error, participant_id=self.participant_id,
        )


class TransferManager:
    """Track active and recent transfers without persisting file or peer secrets."""

    def __init__(self, *, history_limit: int = 250) -> None:
        self._lock = threading.RLock()
        self._transfers: OrderedDict[str, _Transfer] = OrderedDict()
        self._history_limit = max(10, history_limit)

    def create(self, *, filename: str, direction: TransferDirection, total: int,
               participant_id: str, transfer_id: str | None = None) -> TransferView:
        if total < 0:
            raise ValueError("Transfer size cannot be negative.")
        record = _Transfer(filename=filename, direction=direction, total=total,
                           participant_id=participant_id, transfer_id=transfer_id)
        with self._lock:
            if record.transfer_id in self._transfers:
                raise ValueError("Transfer ID is already in use.")
            self._transfers[record.transfer_id] = record
            self._prune()
            return record.view()

    def begin(self, transfer_id: str) -> None:
        with self._lock:
            record = self._get(transfer_id)
            if record.status is not TransferStatus.QUEUED:
                raise TransferCancelled("Transfer is no longer active.")
            record.status = TransferStatus.IN_PROGRESS

    def set_total(self, transfer_id: str, total: int) -> TransferView:
        if total < 0:
            raise ValueError("Transfer size cannot be negative.")
        with self._lock:
            record = self._get(transfer_id)
            if record.transferred or total < record.transferred:
                raise ValueError("Transfer size cannot be reduced below transferred bytes.")
            record.total = total
            return record.view()

    def advance(self, transfer_id: str, amount: int) -> int:
        if amount < 0:
            raise ValueError("Progress cannot move backwards.")
        with self._lock:
            record = self._get(transfer_id)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer was cancelled.")
            if record.status not in {TransferStatus.QUEUED, TransferStatus.IN_PROGRESS}:
                raise TransferCancelled("Transfer is no longer active.")
            record.transferred += amount
            if record.transferred > record.total:
                raise ValueError("Transferred bytes exceed the declared file size.")
            record.status = TransferStatus.IN_PROGRESS
            return record.transferred

    def complete(self, transfer_id: str) -> TransferView:
        with self._lock:
            record = self._get(transfer_id)
            if record.cancel_event.is_set():
                raise TransferCancelled("Transfer was cancelled.")
            if record.transferred != record.total:
                raise ValueError("Transfer cannot complete before all bytes arrive.")
            record.status = TransferStatus.COMPLETED
            record.completed_at = datetime.now(timezone.utc)
            self._prune()
            return record.view()

    def fail(self, transfer_id: str, message: str) -> TransferView:
        return self._finish(transfer_id, TransferStatus.FAILED, message)

    def cancel(self, transfer_id: str, *, participant_id: str | None = None) -> TransferView:
        with self._lock:
            record = self._get(transfer_id)
            if participant_id is not None and record.participant_id != participant_id:
                raise TransferNotFound(transfer_id)
            if record.status in {TransferStatus.QUEUED, TransferStatus.IN_PROGRESS}:
                record.cancel_event.set()
                record.status = TransferStatus.CANCELLED
                record.completed_at = datetime.now(timezone.utc)
            self._prune()
            return record.view()

    def cancel_active(self, *, direction: TransferDirection | None = None) -> None:
        with self._lock:
            for record in self._transfers.values():
                if record.status in {TransferStatus.QUEUED, TransferStatus.IN_PROGRESS} and (
                    direction is None or record.direction is direction
                ):
                    record.cancel_event.set()
                    record.status = TransferStatus.CANCELLED
                    record.completed_at = datetime.now(timezone.utc)

    def cancellation_event(self, transfer_id: str) -> threading.Event:
        with self._lock:
            return self._get(transfer_id).cancel_event

    def get(self, transfer_id: str, *, participant_id: str | None = None) -> TransferView:
        with self._lock:
            record = self._get(transfer_id)
            if participant_id is not None and record.participant_id != participant_id:
                raise TransferNotFound(transfer_id)
            return record.view()

    def list(self, *, participant_id: str | None = None) -> list[TransferView]:
        with self._lock:
            records = list(self._transfers.values())
            if participant_id is not None:
                records = [item for item in records if item.participant_id == participant_id]
            return [item.view() for item in reversed(records)]

    def _finish(self, transfer_id: str, status: TransferStatus, message: str) -> TransferView:
        with self._lock:
            record = self._get(transfer_id)
            if record.status in {TransferStatus.QUEUED, TransferStatus.IN_PROGRESS}:
                record.status = status
                record.error = message[:200]
                record.completed_at = datetime.now(timezone.utc)
            self._prune()
            return record.view()

    def _get(self, transfer_id: str) -> _Transfer:
        record = self._transfers.get(transfer_id)
        if record is None:
            raise TransferNotFound(transfer_id)
        return record

    def _prune(self) -> None:
        terminal = [
            key for key, item in self._transfers.items()
            if item.status in {TransferStatus.COMPLETED, TransferStatus.FAILED, TransferStatus.CANCELLED}
        ]
        while len(terminal) > self._history_limit:
            self._transfers.pop(terminal.pop(0), None)
