"""Offline filesystem monitoring confined to the selected shared root.

Watchdog callbacks only enqueue paths. A separate worker re-lists through
``SharedDirectoryManager`` after a short quiet period, so raw platform events
never become public session events and symlinks/reparse points remain excluded.
"""

from __future__ import annotations

import logging
import os
import queue
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from sharespace.core.file_manager import FileAccessError, SharedDirectoryManager
from sharespace.core.models import FileItemType

logger = logging.getLogger(__name__)
_TEMP_SUFFIXES = (".part", ".partial", ".tmp", ".temp")


@dataclass(frozen=True)
class _FileSignature:
    size_bytes: int
    modified_ns: int
    modified_at: datetime


class _QueuedHandler(FileSystemEventHandler):
    """Keep OS observer callbacks short; all work happens in FileMonitor."""

    def __init__(self, event_queue: queue.Queue[tuple[str, str, str | None, bool]], overflow: threading.Event):
        super().__init__()
        self._queue = event_queue
        self._overflow = overflow

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type == "opened":
            return
        destination = getattr(event, "dest_path", None)
        try:
            self._queue.put_nowait((event.event_type, event.src_path, destination, event.is_directory))
        except queue.Full:
            self._overflow.set()


class FileMonitor:
    """Watch one selected root and emit normalized, safe file change hints."""

    def __init__(
        self,
        directories: SharedDirectoryManager,
        on_event: Callable[[int, dict[str, object]], None],
        on_root_state: Callable[[int, bool], None],
        *,
        debounce_seconds: float = 0.25,
        poll_interval_seconds: float = 0.5,
        queue_size: int = 4096,
    ) -> None:
        if debounce_seconds < 0.05 or poll_interval_seconds < 0.1 or queue_size < 1:
            raise ValueError("File monitor timing and queue settings are invalid.")
        self.directories = directories
        self.on_event = on_event
        self.on_root_state = on_root_state
        self.debounce_seconds = debounce_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._queue_size = queue_size
        self._lock = threading.RLock()
        self._generation = 0
        self._root: Path | None = None
        self._observer: Observer | None = None
        self._watches: dict[str, object] = {}
        self._event_queue: queue.Queue[tuple[str, str, str | None, bool]] | None = None
        self._overflow = threading.Event()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._snapshot: dict[str, _FileSignature] = {}
        self._known_directories: set[str] = set()
        self._scanned_directories: set[str] = set()
        self._root_available = False

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def set_root(self, root: Path | None) -> int:
        """Replace the monitored root, shutting down its old observer first."""
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._root = root
            self._root_available = False
        if root is None:
            return generation
        selected = self.directories.root
        if selected is None or not self._same_path(root, selected):
            return generation
        event_queue: queue.Queue[tuple[str, str, str | None, bool]] = queue.Queue(self._queue_size)
        handler = _QueuedHandler(event_queue, self._overflow)
        self._event_queue = event_queue
        self._stop.clear()
        if not self.directories.root_available():
            self._start_processor(generation, handler)
            return generation
        try:
            initial = self._scan()
            observer = Observer()
            self._observer = observer
            self._watches = {}
            self._snapshot = initial
            self._known_directories = set(self._scanned_directories)
            self._schedule_watches(handler)
            observer.start()
            with self._lock:
                self._root_available = True
                self._stop.clear()
            # A second listing closes the small gap between the baseline and
            # starting platform watches; the resulting diff uses the same path.
            self._apply_scan(self._scan())
            self._start_processor(generation, handler)
        except Exception:
            logger.exception("Could not start the shared-directory observer; it will retry.")
            observer, self._observer = self._observer, None
            if observer is not None:
                observer.stop()
                observer.join(timeout=3)
            self._snapshot.clear()
            self._known_directories.clear()
            self._start_processor(generation, handler)
        return generation

    def _start_processor(self, generation: int, handler: _QueuedHandler) -> None:
        self._worker = threading.Thread(
            target=self._run,
            args=(generation, handler),
            name="sharespace-file-event-processor",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        """Stop observer and worker, discard pending paths, and release watches."""
        with self._lock:
            observer, worker = self._observer, self._worker
            self._observer = None
            self._worker = None
            self._root = None
            self._root_available = False
            self._stop.set()
        if observer is not None:
            observer.stop()
            observer.join(timeout=3)
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
        if worker is not None and worker.is_alive():
            logger.error("Shared-directory event processor did not stop in time.")
        self._watches.clear()
        self._event_queue = None
        self._snapshot.clear()
        self._known_directories.clear()
        self._scanned_directories.clear()
        self._overflow.clear()

    def _run(self, generation: int, handler: _QueuedHandler) -> None:
        while not self._stop.is_set():
            events: list[tuple[str, str, str | None, bool]] = []
            event_queue = self._event_queue
            if event_queue is None:
                return
            try:
                events.append(event_queue.get(timeout=self.poll_interval_seconds))
                quiet_deadline = time.monotonic() + self.debounce_seconds
                while not self._stop.is_set():
                    remaining = quiet_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        events.append(event_queue.get(timeout=remaining))
                        quiet_deadline = time.monotonic() + self.debounce_seconds
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if self._stop.is_set() or generation != self.generation:
                return
            if not self._selected_root_matches():
                self._stop_observer_only()
                return
            if not self.directories.root_available():
                if self._root_available:
                    self._root_available = False
                    self._stop_observer_only()
                    self.on_root_state(generation, False)
                continue
            if not self._root_available:
                try:
                    self._start_observer(handler)
                    self._snapshot = self._scan()
                    self._known_directories = set(self._scanned_directories)
                    self._root_available = True
                    self.on_root_state(generation, True)
                    self.on_event(generation, {"type": "shared_directory_changed", "available": True,
                                               "directory_name": self._root.name if self._root else None})
                except Exception:
                    logger.debug("Shared-directory observer is still unavailable.", exc_info=True)
                continue
            overflowed = self._overflow.is_set()
            if overflowed:
                self._overflow.clear()
            if not events and not overflowed:
                continue
            try:
                directory_event = any(item[3] for item in events)
                parents = {
                    parent
                    for _event_type, source, destination, _is_directory in events
                    for raw_path in (source, destination)
                    if raw_path
                    for parent in (self._event_parent(raw_path),)
                    if parent is not None
                }
                full_scan = overflowed or directory_event or not parents
                current = self._scan() if full_scan else self._scan_shallow(parents)
                # Require a stable file signature across a short second sample.
                if events:
                    time.sleep(min(0.08, self.debounce_seconds / 2))
                    current = self._scan() if full_scan else self._scan_shallow(parents)
                self._apply_scan(current)
                if full_scan:
                    self._schedule_watches(handler)
                if overflowed:
                    self.on_event(generation, {"type": "session_resync_required"})
            except Exception:
                logger.debug("Shared-directory scan failed; retaining the prior snapshot.", exc_info=True)

    def _start_observer(self, handler: _QueuedHandler) -> None:
        root = self._root
        if root is None or not self._selected_root_matches() or not self.directories.root_available():
            return
        observer = Observer()
        self._observer = observer
        self._watches = {}
        self._schedule_watches(handler, observer=observer)
        observer.start()

    def _stop_observer_only(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=3)
        self._watches.clear()

    def _schedule_watches(self, handler: _QueuedHandler, *, observer: Observer | None = None) -> None:
        observer = observer or self._observer
        root = self._root
        if (
            observer is None
            or root is None
            or not self._selected_root_matches()
            or not self.directories.root_available()
        ):
            return
        permitted: dict[str, Path] = {"": root}
        pending = [""]
        while pending:
            relative = pending.pop()
            try:
                listing = self.directories.listing(relative)
            except (FileAccessError, OSError):
                continue
            for item in listing.items:
                if item.item_type is FileItemType.DIRECTORY and item.accessible:
                    permitted[item.relative_path] = root.joinpath(*item.relative_path.split("/"))
                    pending.append(item.relative_path)
        for relative, path in permitted.items():
            if relative in self._watches:
                continue
            try:
                path = self.directories.resolve(relative)
                if not path.is_dir():
                    continue
                # Every directory is scheduled independently. Watchdog is never
                # asked to recurse into links or reparse points on either OS.
                self._watches[relative] = observer.schedule(handler, str(path), recursive=False)
            except (OSError, RuntimeError):
                logger.debug("Could not watch a permitted directory.", exc_info=True)
        for relative in tuple(self._watches):
            if relative not in permitted:
                watch = self._watches.pop(relative)
                try:
                    observer.unschedule(watch)
                except (KeyError, RuntimeError):
                    pass

    def _scan(self) -> dict[str, _FileSignature]:
        root = self._root
        if root is None or not self._selected_root_matches() or not self.directories.root_available():
            raise FileAccessError("The selected shared directory is unavailable.", "sharing_unavailable", 404)
        found: dict[str, _FileSignature] = {}
        found_directories: set[str] = {""}
        pending = [""]
        inaccessible: list[str] = []
        while pending:
            relative_dir = pending.pop()
            try:
                listing = self.directories.listing(relative_dir)
            except (FileAccessError, OSError):
                inaccessible.append(relative_dir)
                continue
            for item in listing.items:
                if self._is_temporary_name(item.filename):
                    continue
                if item.item_type is FileItemType.DIRECTORY:
                    found_directories.add(item.relative_path)
                    if item.accessible:
                        pending.append(item.relative_path)
                    else:
                        inaccessible.append(item.relative_path)
                    continue
                if not item.accessible:
                    inaccessible.append(item.relative_path)
                    continue
                try:
                    _, descriptor = self.directories.safe_open(item.relative_path)
                    try:
                        info = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    found[item.relative_path] = _FileSignature(
                        size_bytes=info.st_size,
                        modified_ns=getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
                        modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc),
                    )
                except (FileAccessError, OSError, ValueError):
                    continue
        # An unreadable subtree is not evidence that its known files vanished.
        for directory in inaccessible:
            prefix = f"{directory}/" if directory else ""
            found.update({
                path: sig for path, sig in self._snapshot.items()
                if path == directory or path.startswith(prefix)
            })
            found_directories.update(
                path for path in self._known_directories
                if path == directory or path.startswith(prefix)
            )
        self._scanned_directories = found_directories
        return found

    def _scan_shallow(self, relative_directories: set[str]) -> dict[str, _FileSignature]:
        """Rescan only event parent directories for ordinary file changes."""
        current = dict(self._snapshot)
        self._scanned_directories = set(self._known_directories)
        for relative in relative_directories:
            prefix = f"{relative}/" if relative else ""
            old_direct = {
                path: signature for path, signature in self._snapshot.items()
                if path.rpartition("/")[0] == relative
            }
            try:
                listing = self.directories.listing(relative)
            except (FileAccessError, OSError):
                continue
            for path in old_direct:
                current.pop(path, None)
            for item in listing.items:
                if item.item_type is not FileItemType.FILE or self._is_temporary_name(item.filename):
                    continue
                if not item.accessible:
                    if item.relative_path in old_direct:
                        current[item.relative_path] = old_direct[item.relative_path]
                    continue
                try:
                    _, descriptor = self.directories.safe_open(item.relative_path)
                    try:
                        info = os.fstat(descriptor)
                    finally:
                        os.close(descriptor)
                    if stat.S_ISREG(info.st_mode):
                        current[item.relative_path] = _FileSignature(
                            size_bytes=info.st_size,
                            modified_ns=getattr(info, "st_mtime_ns", int(info.st_mtime * 1_000_000_000)),
                            modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc),
                        )
                except (FileAccessError, OSError, ValueError):
                    if item.relative_path in old_direct:
                        current[item.relative_path] = old_direct[item.relative_path]
        return current

    def _event_parent(self, raw_path: str) -> str | None:
        root = self._root
        if root is None:
            return None
        try:
            candidate = Path(os.path.abspath(raw_path))
            relative = candidate.relative_to(root).as_posix()
        except (OSError, ValueError):
            return None
        if not self._valid_public_relative_path(relative):
            return None
        return relative.rpartition("/")[0]

    def _apply_scan(self, current: dict[str, _FileSignature]) -> None:
        previous = self._snapshot
        previous_directories = self._known_directories
        current_directories = self._scanned_directories
        changes: list[tuple[str, str, _FileSignature | None]] = []
        for path in previous.keys() - current.keys():
            changes.append(("file_deleted", path, previous[path]))
        for path in current.keys() - previous.keys():
            changes.append(("file_created", path, current[path]))
        for path in previous.keys() & current.keys():
            if previous[path] != current[path]:
                changes.append(("file_modified", path, current[path]))
        self._snapshot = current
        self._known_directories = set(current_directories)
        for event_type, path, signature in sorted(changes, key=lambda item: (item[1], item[0])):
            if not self._valid_public_relative_path(path):
                continue
            payload: dict[str, object] = {
                "type": event_type,
                "filename": path.rsplit("/", 1)[-1],
                "relative_path": path,
                "parent_path": path.rpartition("/")[0],
            }
            if signature is not None:
                payload["size_bytes"] = signature.size_bytes
                payload["modified_at"] = signature.modified_at.isoformat()
            self.on_event(self.generation, payload)
        directory_changes = {
            ("created", path) for path in current_directories - previous_directories if path
        } | {
            ("deleted", path) for path in previous_directories - current_directories if path
        }
        root_name = self._root.name if self._root else None
        for change, relative in sorted(directory_changes, key=lambda item: (item[1], item[0])):
            if not self._valid_public_relative_path(relative):
                continue
            self.on_event(self.generation, {
                "type": "shared_directory_changed",
                "available": True,
                "directory_name": root_name,
                "relative_path": relative,
                "change": change,
            })

    def _valid_public_relative_path(self, relative: str) -> bool:
        if not relative or relative.startswith("/") or "\\" in relative or ":" in relative:
            return False
        parts = relative.split("/")
        if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
            return False
        if any(self._is_temporary_name(part) for part in parts):
            return False
        return self._selected_root_matches() and self.directories.root_available()

    def _selected_root_matches(self) -> bool:
        root = self._root
        selected = self.directories.root
        return root is not None and selected is not None and self._same_path(root, selected)

    @staticmethod
    def _is_temporary_name(name: str) -> bool:
        lower = name.casefold()
        return (
            name.startswith(".sharespace-")
            or name.startswith("~$")
            or name.startswith(".~")
            or lower.endswith(_TEMP_SUFFIXES)
        )

    @staticmethod
    def _same_path(first: Path, second: Path) -> bool:
        return os.path.normcase(os.path.normpath(str(first))) == os.path.normcase(os.path.normpath(str(second)))
