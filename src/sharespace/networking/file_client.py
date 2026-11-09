"""Asynchronous HTTP client for ShareSpace file browsing and transfers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from sharespace.core.file_manager import MAX_FILE_SIZE_BYTES
from sharespace.core.models import DirectoryListing, SharedDirectoryState, TransferDirection, TransferView
from sharespace.core.transfer_manager import TransferCancelled, TransferManager
from sharespace.networking.session_server import API_PREFIX

ProgressCallback = Callable[[TransferView], None]


class FileClientError(RuntimeError):
    """A file API request failed or returned an invalid response."""


class FileClient:
    """Perform authenticated file API calls without Qt dependencies."""

    def __init__(self, host: str, port: int, token: str, *, timeout_seconds: float = 15.0) -> None:
        self.base_url = f"http://{host}:{port}{API_PREFIX}"
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transfers = TransferManager()

    async def settings(self) -> SharedDirectoryState:
        return SharedDirectoryState.model_validate(await self._json("GET", "/files/settings"))

    async def browse(self, relative_path: str = "") -> DirectoryListing:
        return DirectoryListing.model_validate(
            await self._json("GET", "/files", params={"path": relative_path})
        )

    async def transfers_list(self) -> list[TransferView]:
        data = await self._json("GET", "/files/transfers")
        return [TransferView.model_validate(item) for item in data]

    async def cancel(self, transfer_id: str) -> TransferView:
        data = await self._json("POST", f"/files/transfers/{transfer_id}/cancel")
        try:
            self.transfers.cancel(transfer_id)
        except LookupError:
            pass
        return TransferView.model_validate(data)

    async def download(
        self,
        relative_path: str,
        destination: str | os.PathLike[str],
        *,
        cancel_event=None,
        on_progress: ProgressCallback | None = None,
        transfer_id: str | None = None,
    ) -> TransferView:
        identifier = transfer_id or uuid.uuid4().hex
        name = relative_path.rsplit("/", 1)[-1]
        transfer = self.transfers.create(
            filename=name, direction=TransferDirection.DOWNLOAD, total=0,
            participant_id="local", transfer_id=identifier,
        )
        destination_path = Path(destination)
        if not destination_path.parent.exists() or not destination_path.parent.is_dir():
            self.transfers.fail(identifier, "The destination directory is unavailable.")
            raise FileClientError("The destination directory is unavailable.")
        temporary: Path | None = None
        descriptor: int | None = None
        try:
            timeout = httpx.Timeout(self.timeout_seconds, read=None)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "GET",
                    f"{self.base_url}/files/download",
                    params={"path": relative_path},
                    headers=self._headers({"X-Transfer-ID": identifier}),
                ) as response:
                    self._raise_for_status(response)
                    try:
                        total = int(response.headers.get("content-length", "-1"))
                    except ValueError as error:
                        raise FileClientError("The host returned an invalid file size.") from error
                    if total < 0 or total > MAX_FILE_SIZE_BYTES:
                        raise FileClientError("The host returned a file outside the 20 MB limit.")
                    transfer = self.transfers.set_total(identifier, total)
                    self.transfers.begin(identifier)
                    descriptor, temp_name = tempfile.mkstemp(
                        prefix=".sharespace-", suffix=".part", dir=destination_path.parent
                    )
                    temporary = Path(temp_name)
                    received = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            raise TransferCancelled("Download was cancelled.")
                        received += len(chunk)
                        if received > total or received > MAX_FILE_SIZE_BYTES:
                            raise FileClientError("The host sent more bytes than declared.")
                        await asyncio.to_thread(_write_all, descriptor, chunk)
                        self.transfers.advance(identifier, len(chunk))
                        _notify(on_progress, self.transfers.get(identifier))
                    if received != total:
                        raise FileClientError("Download ended before all bytes arrived.")
                    await asyncio.to_thread(os.fsync, descriptor)
                    await asyncio.to_thread(os.close, descriptor)
                    descriptor = None
                    _finalize_without_overwrite(temporary, destination_path)
                    temporary = None
                    result = self.transfers.complete(identifier)
                    _notify(on_progress, result)
                    return result
        except asyncio.CancelledError:
            current = _safe_get(self.transfers, identifier)
            if current and current.status.value in {"QUEUED", "IN_PROGRESS"}:
                _notify(on_progress, self.transfers.cancel(identifier))
            raise
        except Exception as error:
            current = _safe_get(self.transfers, identifier)
            if current and current.status.value in {"QUEUED", "IN_PROGRESS"}:
                if isinstance(error, (TransferCancelled, asyncio.CancelledError)):
                    result = self.transfers.cancel(identifier)
                else:
                    result = self.transfers.fail(identifier, _safe_error(error))
                _notify(on_progress, result)
            if isinstance(error, FileClientError | TransferCancelled | asyncio.CancelledError):
                raise
            raise FileClientError("Could not download the file from the host.") from error
        finally:
            if descriptor is not None:
                await asyncio.to_thread(os.close, descriptor)
            if temporary is not None:
                await asyncio.to_thread(temporary.unlink, missing_ok=True)

    async def upload(
        self,
        source: str | os.PathLike[str],
        relative_directory: str = "",
        *,
        cancel_event=None,
        on_progress: ProgressCallback | None = None,
        transfer_id: str | None = None,
    ) -> TransferView:
        path = Path(source)
        try:
            info = await asyncio.to_thread(path.stat)
        except OSError as error:
            raise FileClientError("The selected file is unavailable.") from error
        if not path.is_file() or info.st_size > MAX_FILE_SIZE_BYTES:
            raise FileClientError("Uploads are limited to 20 MB per file.")
        identifier = transfer_id or uuid.uuid4().hex
        self.transfers.create(
            filename=path.name, direction=TransferDirection.UPLOAD, total=info.st_size,
            participant_id="local", transfer_id=identifier,
        )
        self.transfers.begin(identifier)
        sent = 0

        async def chunks() -> AsyncIterator[bytes]:
            nonlocal sent
            with path.open("rb") as stream:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise TransferCancelled("Upload was cancelled.")
                    chunk = await asyncio.to_thread(stream.read, 64 * 1024)
                    if not chunk:
                        break
                    sent += len(chunk)
                    if sent > info.st_size:
                        raise FileClientError("The selected file changed during upload.")
                    self.transfers.advance(identifier, len(chunk))
                    _notify(on_progress, self.transfers.get(identifier))
                    yield chunk
                if sent != info.st_size:
                    raise FileClientError("The selected file changed during upload.")

        try:
            timeout = httpx.Timeout(self.timeout_seconds, read=None, write=None)
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/files/upload",
                    params={"path": relative_directory},
                    headers=self._headers({
                        "X-Transfer-ID": identifier,
                        "X-File-Size": str(info.st_size),
                        "X-Filename": _encode_filename(path.name),
                        "Content-Type": "application/octet-stream",
                    }),
                    content=chunks(),
                )
            self._raise_for_status(response)
            result = self.transfers.complete(identifier)
            _notify(on_progress, result)
            return result
        except asyncio.CancelledError:
            current = _safe_get(self.transfers, identifier)
            if current and current.status.value in {"QUEUED", "IN_PROGRESS"}:
                _notify(on_progress, self.transfers.cancel(identifier))
            raise
        except Exception as error:
            current = _safe_get(self.transfers, identifier)
            if current and current.status.value in {"QUEUED", "IN_PROGRESS"}:
                if isinstance(error, (TransferCancelled, asyncio.CancelledError)):
                    result = self.transfers.cancel(identifier)
                else:
                    result = self.transfers.fail(identifier, _safe_error(error))
                _notify(on_progress, result)
            if isinstance(error, FileClientError | TransferCancelled | asyncio.CancelledError):
                raise
            raise FileClientError("Could not upload the file to the host.") from error

    async def _json(self, method: str, path: str, *, params: dict[str, str] | None = None):
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(
                    method, f"{self.base_url}{path}", params=params, headers=self._headers()
                )
        except httpx.HTTPError as error:
            raise FileClientError("Could not reach the ShareSpace session host.") from error
        self._raise_for_status(response)
        try:
            return response.json()
        except ValueError as error:
            raise FileClientError("The host returned an invalid response.") from error

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.token}"}
        headers.update(extra or {})
        return headers

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if not response.is_error:
            return
        try:
            data = response.json()
        except ValueError:
            data = {}
        if isinstance(data, dict):
            detail = data.get("detail", "The file request failed.")
            raise FileClientError(str(detail))
        raise FileClientError("The file request failed.")


def _encode_filename(filename: str) -> str:
    import base64

    return base64.urlsafe_b64encode(filename.encode("utf-8")).decode("ascii").rstrip("=")


def _finalize_without_overwrite(temporary: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("Transfer could not be written.")
        view = view[written:]


def _notify(callback: ProgressCallback | None, transfer: TransferView) -> None:
    if callback is not None:
        callback(transfer)


def _safe_get(manager: TransferManager, transfer_id: str) -> TransferView | None:
    try:
        return manager.get(transfer_id)
    except LookupError:
        return None


def _safe_error(error: Exception) -> str:
    return str(error)[:200] or "Transfer failed."
