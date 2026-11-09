"""Confined, read-only access to a host-selected shared directory."""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from sharespace.core.models import DirectoryListing, FileItem, FileItemType, SharedDirectoryState

MAX_FILE_SIZE_BYTES = 20_000_000
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class FileAccessError(RuntimeError):
    """A safe, user-facing file operation failure."""

    def __init__(self, message: str, code: str = "file_unavailable", status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SharedDirectoryManager:
    """Own the one explicitly selected root and enforce path confinement."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._root: Path | None = None

    @property
    def root(self) -> Path | None:
        with self._lock:
            return self._root

    def select_root(self, path: str | os.PathLike[str]) -> SharedDirectoryState:
        candidate = Path(path).expanduser()
        try:
            info = candidate.lstat()
            if self._is_link_or_reparse(info):
                raise FileAccessError("Choose a regular directory, not a link.", "invalid_root")
            root = candidate.resolve(strict=True)
        except FileAccessError:
            raise
        except (OSError, RuntimeError) as error:
            raise FileAccessError("The selected directory is unavailable.", "invalid_root") from error
        if not root.is_dir():
            raise FileAccessError("Choose an existing directory to share.", "invalid_root")
        with self._lock:
            self._root = root
        return self.state()

    def stop_sharing(self) -> SharedDirectoryState:
        with self._lock:
            self._root = None
        return self.state()

    def state(self, uploads_enabled: bool = False) -> SharedDirectoryState:
        with self._lock:
            root = self._root
        return SharedDirectoryState(
            shared=root is not None,
            available=root is not None and root.is_dir(),
            directory_name=root.name if root is not None else None,
            uploads_enabled=uploads_enabled and root is not None,
        )

    def listing(self, relative_path: str = "") -> DirectoryListing:
        directory = self.resolve(relative_path)
        if not directory.is_dir():
            raise FileAccessError("The requested item is not a directory.", "not_a_directory", 404)
        root = self._require_root()
        items: list[FileItem] = []
        try:
            children = list(directory.iterdir())
        except OSError as error:
            raise FileAccessError("This directory cannot be read.", "directory_unavailable", 403) from error
        for child in children:
            if self._is_hidden(child):
                continue
            rel = child.relative_to(root).as_posix()
            try:
                info = child.lstat()
                if self._is_link_or_reparse(info):
                    continue
                is_directory = stat.S_ISDIR(info.st_mode)
                is_regular = stat.S_ISREG(info.st_mode)
                if not is_directory and not is_regular:
                    continue
                resolved = child.resolve(strict=True)
                self._ensure_contained(resolved, root)
                if is_directory:
                    size = 0
                    accessible, reason = os.access(resolved, os.R_OK | os.X_OK), None
                else:
                    size = info.st_size
                    accessible, reason = os.access(resolved, os.R_OK), None
                if not accessible:
                    reason = "Item is unavailable."
                items.append(FileItem(
                    filename=child.name,
                    relative_path=rel,
                    extension=child.suffix.lower() if is_regular else "",
                    size_bytes=size,
                    modified_at=datetime.fromtimestamp(info.st_mtime, timezone.utc),
                    item_type=FileItemType.DIRECTORY if is_directory else FileItemType.FILE,
                    accessible=accessible,
                    unavailable_reason=reason,
                ))
            except (OSError, RuntimeError, ValueError):
                # A race or inaccessible entry does not expose its target or block the listing.
                continue
        items.sort(key=lambda item: (item.item_type is FileItemType.FILE, item.filename.casefold(), item.filename))
        relative = self._normalize(relative_path, allow_empty=True)
        return DirectoryListing(
            directory_path=relative,
            parent_path=relative.rpartition("/")[0] if "/" in relative else ("" if relative else None),
            shared_directory_name=root.name,
            items=items,
        )

    def resolve(self, relative_path: str, *, expect_file: bool = False) -> Path:
        root = self._require_root()
        relative = self._normalize(relative_path, allow_empty=not expect_file)
        current = root
        try:
            for component in relative.split("/") if relative else ():
                current = current / component
                info = current.lstat()
                if self._is_hidden(current):
                    raise FileAccessError("Hidden items are not shared.", "file_not_found", 404)
                if self._is_link_or_reparse(info):
                    raise FileAccessError("Links cannot be accessed through the shared directory.", "link_rejected", 403)
                current = current.resolve(strict=True)
                self._ensure_contained(current, root)
            if expect_file:
                info = current.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise FileAccessError("The requested item is not a regular file.", "not_a_file", 404)
            return current
        except FileAccessError:
            raise
        except FileNotFoundError as error:
            raise FileAccessError("The requested item was not found.", "file_not_found", 404) from error
        except PermissionError as error:
            raise FileAccessError("The requested item is unavailable.", "file_unavailable", 403) from error
        except (OSError, RuntimeError, ValueError) as error:
            raise FileAccessError("The requested path is unavailable.", "invalid_path", 400) from error

    def safe_open(self, relative_path: str) -> tuple[Path, int]:
        """Open a regular file without following a final-component symlink."""
        path = self.resolve(relative_path, expect_file=True)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise FileAccessError("The requested item is not a regular file.", "not_a_file", 404)
            self._ensure_contained(Path(os.path.realpath(path)), self._require_root())
            return path, descriptor
        except FileAccessError:
            raise
        except OSError as error:
            raise FileAccessError("The requested file is unavailable.", "file_unavailable", 403) from error

    def validate_upload_target(self, relative_directory: str, filename: str) -> tuple[Path, str]:
        if (
            not filename
            or filename in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
        ):
            raise FileAccessError("The upload filename is invalid.", "invalid_filename")
        if any(separator in filename for separator in ("/", "\\", ":")) or Path(filename).name != filename:
            raise FileAccessError("Upload a single filename without a path.", "invalid_filename")
        try:
            if len(filename.encode("utf-8")) > 255:
                raise FileAccessError("The upload filename is too long.", "invalid_filename")
        except UnicodeEncodeError as error:
            raise FileAccessError("The upload filename is invalid.", "invalid_filename") from error
        directory = self.resolve(relative_directory)
        if not directory.is_dir():
            raise FileAccessError("The upload destination is not a directory.", "not_a_directory", 404)
        if self._is_hidden(directory / filename):
            raise FileAccessError("Hidden filenames cannot be uploaded.", "invalid_filename")
        target = directory / filename
        try:
            target.lstat()
        except FileNotFoundError:
            pass
        except OSError as error:
            raise FileAccessError("The destination is unavailable.", "file_unavailable", 403) from error
        else:
            raise FileAccessError("A file with this name already exists.", "destination_exists", 409)
        return target, target.relative_to(self._require_root()).as_posix()

    def ensure_path_is_in_root(self, path: Path) -> None:
        self._ensure_contained(path.resolve(strict=False), self._require_root())

    def _require_root(self) -> Path:
        with self._lock:
            root = self._root
        if root is None or not root.is_dir():
            raise FileAccessError("The host is not sharing a directory.", "sharing_unavailable", 404)
        return root

    @staticmethod
    def _normalize(relative_path: str, *, allow_empty: bool) -> str:
        if not isinstance(relative_path, str) or "\x00" in relative_path or "\\" in relative_path:
            raise FileAccessError("The requested path is invalid.", "invalid_path")
        if not relative_path and allow_empty:
            return ""
        if relative_path.startswith("/") or ":" in relative_path:
            raise FileAccessError("The requested path must be relative.", "invalid_path", 403)
        parts = relative_path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise FileAccessError("The requested path is invalid.", "invalid_path", 403)
        return "/".join(parts)

    @staticmethod
    def _ensure_contained(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as error:
            raise FileAccessError("The requested path escapes the shared directory.", "path_escape", 403) from error

    @staticmethod
    def _is_link_or_reparse(info: os.stat_result) -> bool:
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)

    @staticmethod
    def _is_hidden(path: Path) -> bool:
        if path.name.startswith("."):
            return True
        try:
            return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2))
        except OSError:
            return False
