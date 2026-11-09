"""Participant file browser and transfer controls."""

from __future__ import annotations

import threading
import uuid

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharespace.core.models import DirectoryListing, FileItem, FileItemType, SharedDirectoryState, TransferStatus
from sharespace.networking.file_client import FileClient, FileClientError
from sharespace.ui.session_workers import SessionWorker


class FileBrowserDialog(QDialog):
    """Browse a session's explicitly shared root and move files in either direction."""

    def __init__(self, client: FileClient, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.client = client
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(3)
        self._path = ""
        self._listing: DirectoryListing | None = None
        self._state: SharedDirectoryState | None = None
        self._busy = False
        self._operation_id: str | None = None
        self._cancel_events: dict[str, threading.Event] = {}

        self.setWindowTitle("Shared files")
        self.setMinimumSize(640, 540)
        self.resize(780, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        heading = QLabel("Shared files")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        self._status = QLabel("Connecting to the host file service…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        navigation = QHBoxLayout()
        self._back_button = QPushButton("Up")
        self._back_button.clicked.connect(lambda _checked=False: self._go_up())
        self._path_label = QLabel("/")
        self._path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(lambda _checked=False: self._load_directory())
        navigation.addWidget(self._back_button)
        navigation.addWidget(self._path_label, 1)
        navigation.addWidget(self._refresh_button)
        layout.addLayout(navigation)

        self._items = QListWidget()
        self._items.itemDoubleClicked.connect(self._activate_item)
        self._items.currentItemChanged.connect(lambda _current, _previous: self._update_actions())
        layout.addWidget(self._items, 1)

        actions = QHBoxLayout()
        self._download_button = QPushButton("Download selected file")
        self._download_button.clicked.connect(lambda _checked=False: self._download_selected())
        self._upload_button = QPushButton("Upload file")
        self._upload_button.clicked.connect(lambda _checked=False: self._choose_upload())
        actions.addWidget(self._download_button)
        actions.addWidget(self._upload_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        transfer_header = QHBoxLayout()
        transfer_header.addWidget(QLabel("Transfers"))
        transfer_header.addStretch(1)
        self._cancel_button = QPushButton("Cancel selected")
        self._cancel_button.clicked.connect(lambda _checked=False: self._cancel_selected())
        transfer_header.addWidget(self._cancel_button)
        layout.addLayout(transfer_header)
        self._transfers = QListWidget()
        self._transfers.setMaximumHeight(130)
        self._transfers.currentItemChanged.connect(lambda _current, _previous: self._update_actions())
        layout.addWidget(self._transfers)

        self._transfer_timer = QTimer(self)
        self._transfer_timer.setInterval(500)
        self._transfer_timer.timeout.connect(self._refresh_transfers)
        self._transfer_timer.start()
        QTimer.singleShot(0, self._load_directory)

    def _load_directory(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._status.setText("Loading shared files…")

        async def load():
            state = await self.client.settings()
            listing = await self.client.browse(self._path)
            return state, listing

        worker = SessionWorker(load)
        worker.signals.finished.connect(self._directory_loaded)
        worker.signals.failed.connect(self._operation_failed)
        self._thread_pool.start(worker)
        self._update_actions()

    def _directory_loaded(self, result: object) -> None:
        self._busy = False
        self._state, self._listing = result
        listing: DirectoryListing = self._listing
        self._path = listing.directory_path
        self._path_label.setText(f"/{self._path}" if self._path else "/")
        self._items.clear()
        for file in listing.items:
            marker = "📁" if file.item_type is FileItemType.DIRECTORY else "📄"
            detail = (
                "Folder"
                if file.item_type is FileItemType.DIRECTORY
                else f"{file.size_bytes:,} bytes"
            )
            detail += f" · modified {file.modified_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
            if not file.accessible:
                detail = file.unavailable_reason or "Unavailable"
            row = QListWidgetItem(f"{marker}  {file.filename}    ·    {detail}")
            row.setData(Qt.ItemDataRole.UserRole, file)
            if not file.accessible:
                row.setForeground(Qt.GlobalColor.gray)
            self._items.addItem(row)
        if not self._state.available:
            self._status.setText("The host is not currently sharing a folder.")
        elif self._state.uploads_enabled:
            self._status.setText(f"Browsing {listing.shared_directory_name}. Uploads are enabled.")
        else:
            self._status.setText(f"Browsing {listing.shared_directory_name}. Downloads are available; uploads are disabled.")
        self._update_actions()

    def _operation_failed(self, message: str) -> None:
        self._busy = False
        if self._operation_id:
            self._cancel_events.pop(self._operation_id, None)
        self._operation_id = None
        self._status.setText(f"File operation failed: {message}")
        self._update_actions()

    def _go_up(self) -> None:
        if self._listing is None or self._listing.parent_path is None:
            return
        self._path = self._listing.parent_path
        self._load_directory()

    def _activate_item(self, row: QListWidgetItem) -> None:
        item: FileItem = row.data(Qt.ItemDataRole.UserRole)
        if item.item_type is FileItemType.DIRECTORY and item.accessible:
            self._path = item.relative_path
            self._load_directory()
        elif item.item_type is FileItemType.FILE and item.accessible:
            self._download_selected()

    def _download_selected(self) -> None:
        row = self._items.currentItem()
        if row is None:
            return
        item: FileItem = row.data(Qt.ItemDataRole.UserRole)
        if item.item_type is not FileItemType.FILE or not item.accessible:
            return
        destination, _ = QFileDialog.getSaveFileName(self, "Save shared file", item.filename)
        if not destination:
            return
        self._start_transfer(
            item.filename,
            lambda transfer_id, cancel_event, worker: self.client.download(
                item.relative_path, destination, transfer_id=transfer_id,
                cancel_event=cancel_event, on_progress=worker.signals.progress.emit,
            ),
        )

    def _choose_upload(self) -> None:
        if not self._state or not self._state.uploads_enabled:
            self._status.setText("The host has uploads disabled.")
            return
        source, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload")
        if not source:
            return
        self._start_transfer(
            source.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            lambda transfer_id, cancel_event, worker: self.client.upload(
                source, self._path, transfer_id=transfer_id,
                cancel_event=cancel_event, on_progress=worker.signals.progress.emit,
            ),
            refresh_after=True,
        )

    def _start_transfer(self, name: str, operation, *, refresh_after: bool = False) -> None:
        if self._busy:
            return
        self._busy = True
        transfer_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        self._cancel_events[transfer_id] = cancel_event
        self._operation_id = transfer_id
        self._status.setText(f"Starting transfer: {name}")
        worker = None

        def run():
            return operation(transfer_id, cancel_event, worker)

        worker = SessionWorker(run)
        worker.signals.progress.connect(self._transfer_progress)
        worker.signals.finished.connect(lambda result: self._transfer_finished(result, refresh_after))
        worker.signals.failed.connect(self._operation_failed)
        self._thread_pool.start(worker)
        self._update_actions()

    def _transfer_progress(self, transfer: object) -> None:
        self._status.setText(
            f"{transfer.direction.value.title()} {transfer.filename}: "
            f"{transfer.transferred_bytes:,}/{transfer.total_bytes:,} bytes "
            f"({transfer.progress_percent:.0f}%)"
        )
        self._refresh_transfers()

    def _transfer_finished(self, transfer: object, refresh_after: bool) -> None:
        self._busy = False
        if self._operation_id:
            self._cancel_events.pop(self._operation_id, None)
        self._operation_id = None
        self._status.setText(f"{transfer.direction.value.title()} of {transfer.filename} {transfer.status.value.lower()}.")
        self._refresh_transfers()
        self._update_actions()
        if refresh_after and transfer.status is TransferStatus.COMPLETED:
            self._load_directory()

    def _refresh_transfers(self) -> None:
        current_id = self._transfers.currentItem().data(Qt.ItemDataRole.UserRole) if self._transfers.currentItem() else None
        views = self.client.transfers.list()
        self._transfers.blockSignals(True)
        self._transfers.clear()
        selected = None
        for transfer in views:
            row = QListWidgetItem(
                f"{transfer.direction.value.title()} · {transfer.filename} · "
                f"{transfer.transferred_bytes:,}/{transfer.total_bytes:,} bytes · "
                f"{transfer.progress_percent:.0f}% · {transfer.status.value.replace('_', ' ').title()}"
            )
            row.setData(Qt.ItemDataRole.UserRole, transfer.transfer_id)
            row.setData(Qt.ItemDataRole.UserRole + 1, transfer.status in {
                TransferStatus.QUEUED, TransferStatus.IN_PROGRESS
            })
            self._transfers.addItem(row)
            if transfer.transfer_id == current_id:
                selected = row
        if selected:
            self._transfers.setCurrentItem(selected)
        self._transfers.blockSignals(False)
        self._update_actions()

    def _cancel_selected(self) -> None:
        row = self._transfers.currentItem()
        if row is None:
            return
        identifier = row.data(Qt.ItemDataRole.UserRole)
        cancel_event = self._cancel_events.get(identifier)
        if cancel_event:
            cancel_event.set()
        worker = SessionWorker(lambda: self.client.cancel(identifier))
        worker.signals.failed.connect(lambda message: self._status.setText(f"Cancellation requested: {message}"))
        worker.signals.finished.connect(lambda _result: self._status.setText("Transfer cancellation requested."))
        self._thread_pool.start(worker)

    def _update_actions(self) -> None:
        selected = self._items.currentItem()
        item = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        self._download_button.setEnabled(
            not self._busy and item is not None and item.item_type is FileItemType.FILE and item.accessible
        )
        self._upload_button.setEnabled(
            not self._busy and self._state is not None and self._state.uploads_enabled
        )
        self._refresh_button.setEnabled(not self._busy)
        self._back_button.setEnabled(not self._busy and self._listing is not None and self._listing.parent_path is not None)
        transfer = self._transfers.currentItem()
        self._cancel_button.setEnabled(
            transfer is not None and bool(transfer.data(Qt.ItemDataRole.UserRole + 1))
        )

    def closeEvent(self, event) -> None:
        if self._busy:
            self._status.setText("Wait for the current operation or cancel its transfer before closing.")
            event.ignore()
            return
        self._transfer_timer.stop()
        super().closeEvent(event)
