"""Local transfer and session history views."""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

logger = logging.getLogger(__name__)


def human_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


def _display_time(value: str | None) -> str:
    if not value:
        return "Unknown time"
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return "Unknown time"


class HistoryDialog(QDialog):
    """Show one history table with safe filtering, sorting, and local clearing."""

    def __init__(self, store, kind: str, parent=None) -> None:
        super().__init__(parent)
        if kind not in {"transfers", "sessions"}:
            raise ValueError("Unsupported history type.")
        self.store = store
        self.kind = kind
        self.descending = True
        self.setWindowTitle("Transfer history" if kind == "transfers" else "Session history")
        self.resize(760, 520)
        layout = QVBoxLayout(self)
        heading = QLabel(self.windowTitle())
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        filters = QHBoxLayout()
        self._filter = QComboBox()
        if kind == "transfers":
            for text, value in (
                ("All transfers", "all"), ("Uploads", "UPLOAD"), ("Downloads", "DOWNLOAD"),
                ("Completed", "COMPLETED"), ("Failed", "FAILED"),
                ("Cancelled or interrupted", "CANCELLED_OR_INTERRUPTED"),
            ):
                self._filter.addItem(text, value)
            self._sort = QComboBox()
            self._sort.addItem("Date", "date")
            self._sort.addItem("Filename", "filename")
            filters.addWidget(QLabel("Show"))
            filters.addWidget(self._filter)
            filters.addWidget(QLabel("Sort by"))
            filters.addWidget(self._sort)
        else:
            for text, value in (("All sessions", "all"), ("Hosted", "HOST"), ("Joined", "PARTICIPANT")):
                self._filter.addItem(text, value)
            self._sort = None
            filters.addWidget(QLabel("Show"))
            filters.addWidget(self._filter)
        filters.addStretch(1)
        self._order_button = QPushButton("Newest first")
        self._order_button.clicked.connect(self._toggle_order)
        filters.addWidget(self._order_button)
        layout.addLayout(filters)
        self._empty = QLabel("No history yet.")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        layout.addWidget(self._empty)
        self._rows = QListWidget()
        layout.addWidget(self._rows, 1)
        actions = QHBoxLayout()
        self._clear = QPushButton("Clear history")
        self._clear.clicked.connect(self._clear_history)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(self._clear)
        actions.addStretch(1)
        actions.addWidget(close)
        layout.addLayout(actions)
        self._filter.currentIndexChanged.connect(lambda _index: self.refresh())
        if self._sort is not None:
            self._sort.currentIndexChanged.connect(lambda _index: self.refresh())
        self.refresh()

    def _toggle_order(self) -> None:
        self.descending = not self.descending
        self.refresh()

    def refresh(self) -> None:
        if self.kind == "transfers" and self._sort is not None and self._sort.currentData() == "filename":
            self._order_button.setText("Z to A" if self.descending else "A to Z")
        else:
            self._order_button.setText("Newest first" if self.descending else "Oldest first")
        try:
            selected = self._filter.currentData()
            if self.kind == "transfers":
                direction = selected if selected in {"UPLOAD", "DOWNLOAD"} else None
                status = selected if selected in {"COMPLETED", "FAILED"} else None
                records = self.store.transfer_records(
                    direction=direction,
                    status=status,
                    sort_by=self._sort.currentData() if self._sort else "date",
                    descending=self.descending,
                )
                if selected == "CANCELLED_OR_INTERRUPTED":
                    records = [row for row in records if row["status"] in {"CANCELLED", "INTERRUPTED"}]
            else:
                records = self.store.session_records(
                    role=None if selected == "all" else selected,
                    descending=self.descending,
                )
        except Exception:
            logger.exception("Could not load local %s history.", self.kind)
            self._rows.clear()
            self._rows.hide()
            self._empty.setText("History is unavailable. Check local application storage and reopen this screen.")
            self._empty.show()
            self._clear.setEnabled(False)
            return
        self._rows.clear()
        if not records:
            self._empty.setText("No transfers match this filter." if self.kind == "transfers" else "No previous sessions yet.")
            self._empty.show()
            self._rows.hide()
            self._clear.setEnabled(False)
            return
        self._empty.hide()
        self._rows.show()
        self._clear.setEnabled(True)
        for record in records:
            if self.kind == "transfers":
                text = (
                    f"{record['filename']}  ·  {record['direction'].title()}  ·  "
                    f"{human_size(record['file_size'])}  ·  {record['status'].title()}  ·  "
                    f"{_display_time(record['started_at'])}"
                )
                if record.get("participant_identifier"):
                    text += f"  ·  Participant {record['participant_identifier'][:8]}"
                if record.get("error_summary"):
                    text += f"\n{record['error_summary']}"
            else:
                duration = record.get("duration_seconds")
                if duration is None:
                    duration_text = "Duration unavailable"
                else:
                    hours, remainder = divmod(duration, 3600)
                    minutes, seconds = divmod(remainder, 60)
                    duration_text = f"{hours}h {minutes}m" if hours else f"{minutes}m {seconds}s"
                participants = record.get("participant_count")
                participant_text = f"{participants} participants" if participants is not None else "Participant count unavailable"
                text = (
                    f"{record.get('session_name') or 'ShareSpace session'}  ·  "
                    f"{record['role'].title()}  ·  Host: {record['host_device_name']}\n"
                    f"{_display_time(record['started_at'])}  ·  {duration_text}  ·  "
                    f"{participant_text}  ·  {(record.get('outcome') or 'Unknown').title()}"
                )
            row = QListWidgetItem(text)
            row.setData(Qt.ItemDataRole.UserRole, record.get("record_id", record.get("transfer_id")))
            self._rows.addItem(row)

    def _clear_history(self) -> None:
        noun = "transfer" if self.kind == "transfers" else "session"
        answer = QMessageBox.question(
            self, f"Clear {noun} history?",
            f"Remove saved {noun} history from this device? This does not delete shared files.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        try:
            if self.kind == "transfers":
                self.store.clear_transfer_history()
            else:
                self.store.clear_session_history()
        except Exception:
            logger.exception("Could not clear local %s history.", self.kind)
            QMessageBox.warning(
                self, "History unavailable",
                "History could not be cleared. Check local application storage and try again.",
            )
            return
        self.refresh()
