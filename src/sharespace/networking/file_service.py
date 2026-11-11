"""Per-session local file service for safe, bounded streaming transfers."""

from __future__ import annotations

import asyncio
import mimetypes
import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from sharespace.core.file_manager import FileAccessError, MAX_FILE_SIZE_BYTES, SharedDirectoryManager
from sharespace.core.models import TransferDirection, TransferStatus
from sharespace.core.transfer_manager import TransferCancelled, TransferManager

CHUNK_SIZE = 64 * 1024


class FileService:
    """Own file-sharing settings and file operations for one host session."""

    def __init__(self, *, history_store=None) -> None:
        self.directories = SharedDirectoryManager()
        self.transfers = TransferManager(
            on_change=history_store.save_transfer if history_store is not None else None
        )
        self._lock = threading.RLock()
        self.authorization_gate = asyncio.Lock()
        self._uploads_enabled = False
        self._temporary_files: set[Path] = set()

    @property
    def uploads_enabled(self) -> bool:
        with self._lock:
            enabled = self._uploads_enabled
        return enabled and self.directories.root_available()

    def state(self):
        return self.directories.state(self.uploads_enabled)

    def select_root(self, path: str | os.PathLike[str]):
        result = self.directories.select_root(path)
        self.transfers.cancel_active()
        self.set_uploads_enabled(False)
        return result

    async def select_root_guarded(self, path: str | os.PathLike[str]):
        async with self.authorization_gate:
            return self.select_root(path)

    def stop_sharing(self):
        self.transfers.cancel_active()
        self.set_uploads_enabled(False)
        return self.directories.stop_sharing()

    async def stop_sharing_guarded(self):
        async with self.authorization_gate:
            return self.stop_sharing()

    def set_uploads_enabled(self, enabled: bool):
        with self._lock:
            self._uploads_enabled = bool(enabled) and self.directories.root is not None
            result = self._uploads_enabled
        if not result:
            self.transfers.cancel_active(direction=TransferDirection.UPLOAD)
        return result

    async def set_uploads_enabled_guarded(self, enabled: bool):
        async with self.authorization_gate:
            return self.set_uploads_enabled(enabled)

    async def cancel_participant_uploads(self, participant_id: str) -> None:
        async with self.authorization_gate:
            self.transfers.cancel_active(
                direction=TransferDirection.UPLOAD,
                participant_id=participant_id,
            )

    async def disconnect_participant(self, participant_id: str) -> None:
        async with self.authorization_gate:
            self.transfers.cancel_active(participant_id=participant_id)

    async def cancel_disconnected_participants(self, connected_ids: set[str]) -> None:
        async with self.authorization_gate:
            self.transfers.cancel_disconnected_participants(connected_ids)

    async def cancel_all_transfers(self) -> None:
        async with self.authorization_gate:
            self.transfers.cancel_active()

    async def cancel_transfer(self, transfer_id: str, *, participant_id: str | None = None):
        async with self.authorization_gate:
            return self.transfers.cancel(transfer_id, participant_id=participant_id)

    def browse(self, relative_path: str = ""):
        return self.directories.listing(relative_path)

    def search(self, query: str, *, limit: int = 200):
        return self.directories.search(query, limit=limit)

    def start_download(self, relative_path: str, participant_id: str, transfer_id: str):
        path, descriptor = self.directories.safe_open(relative_path)
        try:
            info = os.fstat(descriptor)
            if info.st_size > MAX_FILE_SIZE_BYTES:
                raise FileAccessError("Files larger than 20 MB cannot be shared.", "file_too_large", 413)
            record = self.transfers.create(
                filename=path.name, direction=TransferDirection.DOWNLOAD,
                total=info.st_size, participant_id=participant_id, transfer_id=transfer_id,
            )
            self.transfers.begin(record.transfer_id)
            return path, descriptor, record
        except Exception:
            os.close(descriptor)
            raise

    async def download_chunks(self, descriptor: int, transfer_id: str):
        try:
            while True:
                if self.transfers.cancellation_event(transfer_id).is_set():
                    raise TransferCancelled("Transfer was cancelled.")
                chunk = await asyncio.to_thread(os.read, descriptor, CHUNK_SIZE)
                if not chunk:
                    break
                self.transfers.advance(transfer_id, len(chunk))
                yield chunk
            self.transfers.complete(transfer_id)
        except (TransferCancelled, asyncio.CancelledError):
            self.transfers.cancel(transfer_id)
            raise
        except Exception as error:
            self.transfers.fail(transfer_id, "The file could not be sent.")
            raise error
        finally:
            await asyncio.to_thread(os.close, descriptor)

    async def receive_upload(self, *, relative_directory: str, filename: str,
                             expected_size: int, transfer_id: str,
                             participant_id: str, body,
                             authorize_upload: Callable[[], object]) -> dict[str, object]:
        if not self.uploads_enabled:
            raise FileAccessError("The host has not enabled uploads.", "uploads_disabled", 403)
        authorize_upload()
        if expected_size < 0 or expected_size > MAX_FILE_SIZE_BYTES:
            raise FileAccessError("Uploads are limited to 20 MB per file.", "file_too_large", 413)
        target, relative_target = self.directories.validate_upload_target(relative_directory, filename)
        record = self.transfers.create(filename=filename, direction=TransferDirection.UPLOAD,
                                       total=expected_size, participant_id=participant_id,
                                       transfer_id=transfer_id)
        self.transfers.begin(record.transfer_id)
        temporary_path: Path | None = None
        descriptor: int | None = None
        written = 0
        try:
            async with self.authorization_gate:
                self._require_upload_authorization(authorize_upload, transfer_id)
                self.directories.ensure_path_is_in_root(target.parent)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".sharespace-", suffix=".part", dir=target.parent
                )
                temporary_path = Path(temporary_name)
                with self._lock:
                    self._temporary_files.add(temporary_path)
            while True:
                if self.transfers.cancellation_event(transfer_id).is_set():
                    raise TransferCancelled("Transfer was cancelled.")
                with self._lock:
                    if not self._uploads_enabled:
                        raise TransferCancelled("The host disabled uploads.")
                chunk = await self._next_upload_chunk(body, transfer_id)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_FILE_SIZE_BYTES or written > expected_size:
                    raise FileAccessError("Upload exceeded its declared size.", "size_mismatch", 413)
                async with self.authorization_gate:
                    self._require_upload_authorization(authorize_upload, transfer_id)
                    await asyncio.to_thread(_write_all, descriptor, chunk)
                    self.transfers.advance(transfer_id, len(chunk))
            if written != expected_size:
                raise FileAccessError("Upload ended before its declared size.", "size_mismatch", 400)
            await asyncio.to_thread(os.fsync, descriptor)
            await asyncio.to_thread(os.close, descriptor)
            descriptor = None
            async with self.authorization_gate:
                self._require_upload_authorization(authorize_upload, transfer_id)
                self.directories.ensure_path_is_in_root(target.parent)
                try:
                    await asyncio.to_thread(_finalize_without_overwrite, temporary_path, target)
                except FileExistsError as error:
                    raise FileAccessError("A file with this name already exists.", "destination_exists", 409) from error
                except OSError as error:
                    raise FileAccessError("The destination cannot safely accept new files.", "destination_unavailable", 409) from error
                temporary_path = None
                self.transfers.complete(transfer_id)
            return {"transfer_id": transfer_id, "relative_path": relative_target, "size_bytes": written}
        except (TransferCancelled, asyncio.CancelledError):
            self.transfers.cancel(transfer_id)
            raise
        except Exception as error:
            self.transfers.fail(transfer_id, "The upload could not be saved.")
            raise error
        finally:
            if descriptor is not None:
                await asyncio.to_thread(os.close, descriptor)
            if temporary_path is not None:
                try:
                    await asyncio.to_thread(temporary_path.unlink, missing_ok=True)
                except OSError:
                    pass
            with self._lock:
                if temporary_path is not None:
                    self._temporary_files.discard(temporary_path)

    async def _next_upload_chunk(self, body, transfer_id: str) -> bytes:
        """Wait for request data while still polling host/participant cancellation."""
        body_task = asyncio.create_task(body())
        try:
            while True:
                if self.transfers.cancellation_event(transfer_id).is_set():
                    raise TransferCancelled("Transfer was cancelled.")
                with self._lock:
                    if not self._uploads_enabled:
                        raise TransferCancelled("The host disabled uploads.")
                try:
                    return await asyncio.wait_for(asyncio.shield(body_task), timeout=0.25)
                except TimeoutError:
                    continue
        finally:
            if not body_task.done():
                body_task.cancel()
                await asyncio.gather(body_task, return_exceptions=True)

    def _require_upload_authorization(
        self,
        authorize_upload: Callable[[], object],
        transfer_id: str,
    ) -> None:
        if self.transfers.cancellation_event(transfer_id).is_set():
            raise TransferCancelled("Transfer was cancelled.")
        with self._lock:
            if not self._uploads_enabled:
                raise TransferCancelled("The host disabled uploads.")
        try:
            authorize_upload()
        except Exception:
            self.transfers.cancel(transfer_id)
            raise

    def cleanup(self) -> None:
        self.transfers.cancel_active()
        with self._lock:
            temporary_files = tuple(self._temporary_files)
        for path in temporary_files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def content_disposition(filename: str) -> str:
    """Return an RFC 5987 attachment header without accepting path syntax."""
    ascii_fallback = "".join(char if 32 <= ord(char) < 127 and char not in '"\\' else "_" for char in filename)
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


def guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Upload could not be written.")
        view = view[written:]


def _finalize_without_overwrite(temporary: Path, target: Path) -> None:
    # Windows rename is atomic and refuses an existing destination. POSIX link
    # creates the final name atomically without replacement, then drops the temp.
    if os.name == "nt":
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()
