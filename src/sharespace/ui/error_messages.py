"""Convert internal failures into concise, path-safe desktop messages."""

from __future__ import annotations

import errno
import re
import sqlite3
import ssl

import httpx

from sharespace.core.file_manager import FileAccessError
from sharespace.core.transfer_manager import TransferCancelled
from sharespace.networking.file_client import FileClientError
from sharespace.networking.session_client import SessionClientError
from sharespace.networking.tls_security import TLSVerificationError

_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\s<>\"|?*]+\\?)*")
_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)*[^\s/]+")


def user_facing_error(error: BaseException) -> str:
    """Return a helpful message without exposing machine-local paths or secrets."""
    if isinstance(error, TLSVerificationError | ssl.SSLCertVerificationError):
        return "The host certificate could not be verified. Compare the fingerprint through a trusted channel."
    if isinstance(error, httpx.TimeoutException | TimeoutError):
        return "The host did not respond in time. Check the local network and try again."
    if isinstance(error, httpx.ConnectError | ConnectionRefusedError):
        return "The host is unavailable. Check that its session is still running."
    if isinstance(error, FileNotFoundError):
        return "That file or folder is no longer available. Refresh and try again."
    if isinstance(error, FileExistsError):
        return "A file with that name already exists. Choose another destination."
    if isinstance(error, PermissionError):
        return "Access was denied. Choose an accessible file or folder and try again."
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
        return "There is not enough free space to complete this operation. Choose another destination."
    if isinstance(error, sqlite3.Error):
        return "Local history or settings are unavailable. Check application storage."
    if isinstance(error, TransferCancelled):
        return "The transfer was cancelled."
    message = str(error)
    message = _WINDOWS_PATH.sub("[local path]", message)
    message = _POSIX_PATH.sub("[local path]", message)
    message = re.sub(r"(?i)bearer\s+\S+|joining code\s*[:=]\s*\S+|token\s*[:=]\s*\S+", "[redacted]", message)
    message = message[:240].strip()
    return message or "The operation could not be completed. Try again."
