"""Small, versioned SQLite store for local settings and metadata history.

This database never stores shared file contents, credentials, or absolute shared
directory paths. Callers may use the store from Qt worker and session threads; all
connection access is serialized by one lock.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths

from sharespace.core.models import TransferView

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 1
_ACTIVE_TRANSFER_STATUSES = ("QUEUED", "IN_PROGRESS")
_SESSION_OUTCOMES = {"COMPLETED", "DISCONNECTED", "INTERRUPTED", "FAILED"}
_SECRET_PATTERN = re.compile(r"(?i)(bearer\s+\S+|joining code\s*[:=]\s*\S+|token\s*[:=]\s*\S+)")
_WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:\\(?:[^\s<>\"|?*]+\\?)*")


def default_database_path() -> Path:
    """Return the platform's application-local data path through Qt's locator."""
    location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not location:
        raise OSError("The operating system did not provide an application data directory.")
    return Path(location) / "sharespace.sqlite3"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean_error(value: str | None) -> str | None:
    if not value:
        return None
    result = _SECRET_PATTERN.sub("[redacted]", value)
    result = _WINDOWS_PATH.sub("[local path]", result)
    # POSIX absolute paths are removed conservatively; user-facing diagnostics stay
    # short and do not need the local machine's directory layout.
    result = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)*[^\s/]+", "[local path]", result)
    return result[:200]


class LocalStore:
    """Thread-safe repository for preferences, transfer history, and session history."""

    def __init__(self, database_path: str | os.PathLike[str] | None = None) -> None:
        selected_path = Path(database_path) if database_path is not None else default_database_path()
        self.persistent = str(selected_path) != ":memory:"
        if self.persistent:
            selected_path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                try:
                    selected_path.parent.chmod(0o700)
                except OSError:
                    logger.warning("Could not restrict permissions on the application data directory.")
        self.path = selected_path
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(selected_path), timeout=5.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self.persistent:
            self._connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        if self.persistent and os.name != "nt":
            try:
                self.path.chmod(0o600)
            except OSError:
                logger.warning("Could not restrict permissions on the local history database.")

    def _migrate(self) -> None:
        with self._lock:
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version > _SCHEMA_VERSION:
                raise sqlite3.DatabaseError("The local data schema is newer than this application.")
            if version == _SCHEMA_VERSION:
                return
            try:
                self._connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS transfer_history (
                        transfer_id TEXT PRIMARY KEY,
                        filename TEXT NOT NULL,
                        direction TEXT NOT NULL CHECK(direction IN ('UPLOAD', 'DOWNLOAD')),
                        file_size INTEGER NOT NULL CHECK(file_size >= 0),
                        transferred_bytes INTEGER NOT NULL CHECK(transferred_bytes >= 0),
                        status TEXT NOT NULL CHECK(status IN
                            ('QUEUED', 'IN_PROGRESS', 'COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED')),
                        started_at TEXT NOT NULL,
                        completed_at TEXT,
                        error_summary TEXT,
                        participant_identifier TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_transfer_history_date
                        ON transfer_history(started_at DESC);
                    CREATE TABLE IF NOT EXISTS session_history (
                        record_id TEXT PRIMARY KEY,
                        session_id TEXT,
                        session_name TEXT,
                        host_device_name TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('HOST', 'PARTICIPANT')),
                        started_at TEXT NOT NULL,
                        ended_at TEXT,
                        duration_seconds INTEGER,
                        participant_count INTEGER,
                        outcome TEXT CHECK(outcome IS NULL OR outcome IN
                            ('ACTIVE', 'COMPLETED', 'DISCONNECTED', 'INTERRUPTED', 'FAILED'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_session_history_date
                        ON session_history(started_at DESC);
                    """
                )
                self._connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                self._connection.commit()
            except sqlite3.DatabaseError:
                self._connection.rollback()
                raise

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value_json FROM settings WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except (TypeError, json.JSONDecodeError):
            logger.warning("Ignoring an invalid stored application setting (%s).", key)
            return default

    def set_setting(self, key: str, value: Any) -> None:
        self.set_settings({key: value})

    def set_settings(self, values: dict[str, Any]) -> None:
        encoded_values = [
            (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")), _iso(utc_now()))
            for key, value in values.items()
        ]
        with self._lock, self._connection:
            self._connection.executemany(
                "INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, "
                "updated_at=excluded.updated_at",
                encoded_values,
            )

    def settings(self, defaults: dict[str, Any]) -> dict[str, Any]:
        return {key: self.get_setting(key, value) for key, value in defaults.items()}

    def save_transfer(self, transfer: TransferView) -> None:
        """Upsert the transfer snapshot; the transfer ID prevents duplicate rows."""
        error_summary = _clean_error(transfer.error)
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO transfer_history(
                    transfer_id, filename, direction, file_size, transferred_bytes,
                    status, started_at, completed_at, error_summary, participant_identifier
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(transfer_id) DO UPDATE SET
                    filename=excluded.filename, direction=excluded.direction,
                    file_size=excluded.file_size, transferred_bytes=excluded.transferred_bytes,
                    status=excluded.status, started_at=excluded.started_at,
                    completed_at=excluded.completed_at, error_summary=excluded.error_summary,
                    participant_identifier=excluded.participant_identifier""",
                (
                    transfer.transfer_id, transfer.filename, transfer.direction.value,
                    transfer.total_bytes, transfer.transferred_bytes, transfer.status.value,
                    _iso(transfer.started_at), _iso(transfer.completed_at), error_summary,
                    transfer.participant_id or None,
                ),
            )

    def transfer_records(
        self, *, direction: str | None = None, status: str | None = None,
        sort_by: str = "date", descending: bool = True,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if direction in {"UPLOAD", "DOWNLOAD"}:
            clauses.append("direction = ?")
            parameters.append(direction)
        if status in {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}:
            clauses.append("status = ?")
            parameters.append(status)
        order_column = "filename COLLATE NOCASE" if sort_by == "filename" else "started_at"
        order_direction = "DESC" if descending else "ASC"
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM transfer_history{where} ORDER BY {order_column} {order_direction}, transfer_id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_transfer_history(self) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM transfer_history WHERE status NOT IN ('QUEUED', 'IN_PROGRESS')"
            )
            return cursor.rowcount

    def start_session(
        self, *, session_id: str | None, session_name: str | None,
        host_device_name: str, role: str, started_at: datetime | None = None,
        participant_count: int | None = None,
    ) -> str:
        if role not in {"HOST", "PARTICIPANT"}:
            raise ValueError("Session role is invalid.")
        record_id = uuid.uuid4().hex
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO session_history(
                    record_id, session_id, session_name, host_device_name, role,
                    started_at, participant_count, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE')""",
                (record_id, session_id, session_name, host_device_name, role,
                 _iso(started_at or utc_now()), participant_count),
            )
        return record_id

    def update_session_participant_count(self, record_id: str, count: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE session_history SET participant_count = MAX(COALESCE(participant_count, 0), ?) "
                "WHERE record_id = ? AND outcome = 'ACTIVE'",
                (max(0, count), record_id),
            )

    def finish_session(
        self, record_id: str, outcome: str, *, ended_at: datetime | None = None
    ) -> None:
        if outcome not in _SESSION_OUTCOMES:
            raise ValueError("Session outcome is invalid.")
        end = ended_at or utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE session_history SET ended_at = ?, duration_seconds = MAX(0,
                    CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)), outcome = ?
                    WHERE record_id = ? AND outcome = 'ACTIVE'""",
                (_iso(end), _iso(end), outcome, record_id),
            )

    def record_failed_session(
        self, *, session_id: str | None, session_name: str | None,
        host_device_name: str, role: str, started_at: datetime | None = None,
    ) -> str:
        record_id = self.start_session(
            session_id=session_id, session_name=session_name,
            host_device_name=host_device_name, role=role, started_at=started_at,
        )
        self.finish_session(record_id, "FAILED")
        return record_id

    def session_records(
        self, *, role: str | None = None, descending: bool = True
    ) -> list[dict[str, Any]]:
        where, parameters = (
            (" WHERE role = ? AND outcome != 'ACTIVE'", [role])
            if role in {"HOST", "PARTICIPANT"}
            else (" WHERE outcome != 'ACTIVE'", [])
        )
        direction = "DESC" if descending else "ASC"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM session_history{where} ORDER BY started_at {direction}, record_id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_session_history(self) -> int:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM session_history WHERE outcome != 'ACTIVE'"
            )
            return cursor.rowcount

    def recover_interrupted(self) -> tuple[int, int]:
        """Finalize rows left active by a prior process termination."""
        now = _iso(utc_now())
        with self._lock, self._connection:
            transfers = self._connection.execute(
                "UPDATE transfer_history SET status='INTERRUPTED', completed_at=?, "
                "error_summary='Application closed before the transfer finished.' "
                "WHERE status IN ('QUEUED', 'IN_PROGRESS')", (now,),
            ).rowcount
            sessions = self._connection.execute(
                """UPDATE session_history SET ended_at = ?,
                    duration_seconds = MAX(0, CAST((julianday(?) - julianday(started_at)) * 86400 AS INTEGER)),
                    outcome = 'INTERRUPTED' WHERE outcome = 'ACTIVE'""",
                (now, now),
            ).rowcount
        return transfers, sessions

    def apply_retention(self, retention_days: int) -> tuple[int, int]:
        """Remove expired terminal history only; zero means keep it indefinitely."""
        if retention_days <= 0:
            return 0, 0
        cutoff = _iso(utc_now() - timedelta(days=retention_days))
        with self._lock, self._connection:
            transfers = self._connection.execute(
                "DELETE FROM transfer_history WHERE status NOT IN ('QUEUED', 'IN_PROGRESS') "
                "AND COALESCE(completed_at, started_at) < ?", (cutoff,),
            ).rowcount
            sessions = self._connection.execute(
                "DELETE FROM session_history WHERE outcome != 'ACTIVE' AND ended_at < ?",
                (cutoff,),
            ).rowcount
        return transfers, sessions

    def close(self) -> None:
        with self._lock:
            self._connection.close()
