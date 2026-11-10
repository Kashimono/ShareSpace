"""Connect confined filesystem monitoring to the authenticated event broker."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sharespace.core.file_monitor import FileMonitor
from sharespace.networking.websocket_events import EventBroker

logger = logging.getLogger(__name__)


class FileEventCoordinator:
    """Own one FileMonitor and marshal its worker-thread events to asyncio."""

    def __init__(self, file_service, broker: EventBroker) -> None:
        self.file_service = file_service
        self.broker = broker
        self.monitor = FileMonitor(
            file_service.directories,
            self._from_monitor,
            self._root_state_from_monitor,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._monitor_generation: int | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        """Start observing a root selected before FastAPI lifespan began."""
        self._loop = asyncio.get_running_loop()
        root = self.file_service.directories.root
        if root is not None:
            await self._replace_root(root, publish_change=False)

    async def set_root(self, root: Path | None) -> None:
        """Replace monitoring after an authorized host root-selection call."""
        self._loop = asyncio.get_running_loop()
        await self._replace_root(root, publish_change=True)

    async def stop(self) -> None:
        """Stop watching while retaining the server's remaining session state."""
        async with self._lock:
            self._closed = True
            self._monitor_generation = None
            await asyncio.to_thread(self.monitor.set_root, None)

    async def close(self) -> None:
        await self.stop()

    async def _replace_root(self, root: Path | None, *, publish_change: bool) -> None:
        async with self._lock:
            if self._closed:
                return
            # Ignore callbacks already queued from the previous root.
            self._monitor_generation = None
            generation = await asyncio.to_thread(self.monitor.set_root, root)
            self._monitor_generation = generation
        if publish_change:
            state = self.file_service.state()
            event_type = (
                "shared_directory_changed"
                if root is None or state.available
                else "shared_directory_unavailable"
            )
            await self.broker.publish({
                "type": event_type,
                "available": state.available,
                "directory_name": state.directory_name,
            })
            await self._publish_file_state()
            # Events may have occurred during observer startup. A fresh API
            # listing after this hint is the authoritative reconnect behavior.
            await self.broker.publish({"type": "session_resync_required"})

    def _from_monitor(self, generation: int, event: dict[str, object]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._handle_monitor_event(generation, event), loop)
            future.add_done_callback(self._log_failed_callback)
        except RuntimeError:
            logger.debug("Discarded a file event after the session loop closed.")

    def _root_state_from_monitor(self, generation: int, available: bool) -> None:
        if available:
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            future = asyncio.run_coroutine_threadsafe(self._handle_root_unavailable(generation), loop)
            future.add_done_callback(self._log_failed_callback)
        except RuntimeError:
            logger.debug("Discarded a root-state event after the session loop closed.")

    async def _handle_monitor_event(self, generation: int, event: dict[str, object]) -> None:
        if generation != self._monitor_generation:
            return
        await self.broker.publish(event)

    async def _handle_root_unavailable(self, generation: int) -> None:
        if (
            generation != self._monitor_generation
            or self.file_service.directories.root_available()
        ):
            return
        async with self.file_service.authorization_gate:
            if self.file_service.directories.root_available():
                return
            self.file_service.set_uploads_enabled(False)
        state = self.file_service.state()
        if state.available:
            await self.broker.publish({
                "type": "shared_directory_changed",
                "available": True,
                "directory_name": state.directory_name,
            })
        else:
            await self.broker.publish({
                "type": "shared_directory_unavailable",
                "available": False,
                "directory_name": state.directory_name,
            })
        await self._publish_file_state()

    async def _publish_file_state(self) -> None:
        state = self.file_service.state()
        await self.broker.publish({
            "type": "file_sharing_state_changed",
            "available": state.available,
            "uploads_enabled": state.uploads_enabled,
            "directory_name": state.directory_name,
        })

    @staticmethod
    def _log_failed_callback(future) -> None:
        try:
            future.result()
        except Exception:
            logger.exception("Could not broadcast a filesystem event.")
