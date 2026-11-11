"""Participant file browser and transfer controls."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharespace.core.models import (
    DirectoryListing,
    FileItem,
    FileItemType,
    PermissionLevel,
    SharedDirectoryState,
    TransferStatus,
)
from sharespace.networking.file_client import FileClient
from sharespace.ui.notification_manager import NotificationManager
from sharespace.ui.session_workers import SessionWorker
from sharespace.ui.history_dialog import HistoryDialog, human_size
from sharespace.ui.app_settings import default_preferences


class FileBrowserDialog(QDialog):
    """Browse a session's explicitly shared root and move files in either direction."""

    def __init__(
        self,
        client: FileClient,
        parent: QWidget | None = None,
        *,
        permission: PermissionLevel = PermissionLevel.READ_ONLY,
        notifications: NotificationManager | None = None,
        store=None,
        preferences: dict[str, object] | None = None,
    ) -> None:
        super().__init__(parent)
        self.client = client
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(3)
        self._path = ""
        self._listing: DirectoryListing | None = None
        self._state: SharedDirectoryState | None = None
        self._permission = permission
        self._store = store
        self._preferences = preferences or default_preferences()
        self._session_unavailable: str | None = None
        self._busy = False
        self._operation_id: str | None = None
        self._active_transfer_ids: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}
        self._live_refresh_pending = False
        self._live_refresh_attempts = 0
        self._search_query = ""
        self._search_results: list[FileItem] = []
        self._search_busy = False
        self._search_pending = False
        self._sort_key = "name"
        self._sort_descending = False

        self.setWindowTitle("Shared files")
        self.setMinimumSize(640, 540)
        self.resize(780, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        heading = QLabel("Shared files")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        self._notifications = notifications or NotificationManager(
            self, layout, native_owner=getattr(self.parent(), "_notifications", None),
            preferences=self._preferences.get("notifications"),
        )
        self._status = QLabel("Connecting to the host file service…")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        search_row = QHBoxLayout()
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText("Search shared files and folders…")
        self._search_input.setMaxLength(128)
        self._search_input.textChanged.connect(self._search_text_changed)
        self._clear_search_button = QPushButton("Clear")
        self._clear_search_button.clicked.connect(self._clear_search)
        self._search_loading = QLabel("Searching…")
        self._search_loading.hide()
        search_row.addWidget(self._search_input, 1)
        search_row.addWidget(self._clear_search_button)
        search_row.addWidget(self._search_loading)
        layout.addLayout(search_row)

        navigation = QHBoxLayout()
        self._back_button = QPushButton("Up")
        self._back_button.clicked.connect(lambda _checked=False: self._go_up())
        self._path_label = QLabel("Current folder: /")
        self._path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(lambda _checked=False: self._refresh_current_view())
        navigation.addWidget(self._back_button)
        navigation.addWidget(self._path_label, 1)
        navigation.addWidget(self._refresh_button)
        layout.addLayout(navigation)

        self._breadcrumbs = QHBoxLayout()
        layout.addLayout(self._breadcrumbs)
        sort_row = QHBoxLayout()
        sort_row.addWidget(QLabel("Sort by"))
        self._sort_combo = QComboBox()
        for label, value in (("Name", "name"), ("Size", "size"),
                             ("Modified", "modified"), ("File type", "type")):
            self._sort_combo.addItem(label, value)
        self._sort_combo.currentIndexChanged.connect(self._sort_changed)
        self._sort_order_button = QPushButton("Ascending")
        self._sort_order_button.clicked.connect(self._toggle_sort_order)
        self._open_location_button = QPushButton("Open result location")
        self._open_location_button.clicked.connect(self._open_search_result_location)
        self._open_location_button.setEnabled(False)
        sort_row.addWidget(self._sort_combo)
        sort_row.addWidget(self._sort_order_button)
        sort_row.addStretch(1)
        sort_row.addWidget(self._open_location_button)
        layout.addLayout(sort_row)

        self._items = QListWidget()
        self._items.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._items.itemDoubleClicked.connect(self._activate_item)
        self._items.currentItemChanged.connect(lambda _current, _previous: self._update_actions())
        layout.addWidget(self._items, 1)
        self._empty_state = QLabel("This shared folder is empty.")
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)
        self._empty_state.hide()
        layout.addWidget(self._empty_state, 1)

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
        self._history_button = QPushButton("History")
        self._history_button.clicked.connect(self._open_transfer_history)
        transfer_header.addWidget(self._history_button)
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
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setSingleShot(True)
        self._live_refresh_timer.setInterval(180)
        self._live_refresh_timer.timeout.connect(self._run_live_refresh)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._run_search)
        QTimer.singleShot(0, self._load_directory)

    def _search_text_changed(self, text: str) -> None:
        self._search_query = text
        self._search_pending = True
        self._search_timer.start()

    def _clear_search(self) -> None:
        if not self._search_input.text():
            return
        self._search_input.clear()

    def _refresh_current_view(self) -> None:
        if self._search_query.strip():
            self._search_timer.start(0)
        else:
            self._load_directory()

    def _run_search(self) -> None:
        query = self._search_query.strip()
        if not query:
            self._search_busy = False
            self._search_results = []
            self._open_location_button.setEnabled(False)
            self._load_directory()
            return
        if self._session_unavailable:
            return
        if self._search_busy:
            self._search_pending = True
            return
        self._search_pending = False
        self._search_busy = True
        self._search_loading.show()
        self._status.setText("Searching the shared folder…")
        worker = SessionWorker(lambda: self.client.search(query))
        worker.signals.finished.connect(lambda result, searched=query: self._search_finished(result, searched))
        worker.signals.failed.connect(lambda message, searched=query: self._search_failed(message, searched))
        self._thread_pool.start(worker)
        self._update_actions()

    def _search_finished(self, result: object, query: str) -> None:
        self._search_busy = False
        self._search_loading.hide()
        if query != self._search_query.strip():
            self._search_pending = True
        else:
            self._search_results = list(result.results)
            self._render_items(self._search_results, search=True)
            suffix = " (showing the first 200 results)" if result.truncated else ""
            self._status.setText(
                f"{len(self._search_results)} result(s) for ‘{query}’{suffix}."
                if self._search_results else f"No results for ‘{query}’."
            )
        self._update_actions()
        if self._search_pending:
            self._search_timer.start(0)

    def _search_failed(self, message: str, query: str) -> None:
        self._search_busy = False
        self._search_loading.hide()
        self._search_results = []
        self._render_items([], search=True)
        self._status.setText(f"Search could not be completed: {message}")
        if "certificate changed" in message.casefold() or "certificate validation" in message.casefold():
            self._session_unavailable = message
        self._update_actions()
        if query != self._search_query.strip():
            self._search_timer.start(0)

    def _render_items(self, items: list[FileItem], *, search: bool = False) -> None:
        self._items.clear()
        ordered = self._sort_items(items)
        for file in ordered:
            folder = file.item_type is FileItemType.DIRECTORY
            kind = "Folder" if folder else (f"{file.extension.upper()[1:]} file" if file.extension else "File")
            detail = kind
            if not folder:
                detail += f" · {human_size(file.size_bytes)} ({file.size_bytes:,} bytes)"
            detail += f" · modified {file.modified_at.astimezone().strftime('%Y-%m-%d %H:%M')}"
            if search:
                parent = file.relative_path.rpartition("/")[0]
                detail += f" · in /{parent}" if parent else " · in shared folder"
            if not file.accessible:
                detail = file.unavailable_reason or "Unavailable"
            marker = "📁" if folder else "📄"
            row = QListWidgetItem(f"{marker}  {file.filename}    ·    {detail}")
            row.setData(Qt.ItemDataRole.UserRole, file)
            row.setToolTip(
                f"{file.relative_path}\n{kind}\n{file.size_bytes:,} bytes\n"
                f"Modified {file.modified_at.astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            if not file.accessible:
                row.setForeground(Qt.GlobalColor.gray)
            self._items.addItem(row)
        if self._items.count():
            self._items.show()
            self._empty_state.hide()
        else:
            self._items.hide()
            self._empty_state.setText(
                "No matching files or folders were found." if search
                else "This shared folder is empty."
            )
            self._empty_state.show()
        self._open_location_button.setEnabled(
            search and self._items.currentItem() is not None and not self._busy
        )

    def _sort_items(self, items: list[FileItem]) -> list[FileItem]:
        folders = [item for item in items if item.item_type is FileItemType.DIRECTORY]
        files = [item for item in items if item.item_type is FileItemType.FILE]
        key = self._sort_key
        if key == "size":
            sort_key = lambda item: (item.size_bytes, item.filename.casefold(), item.filename)
        elif key == "modified":
            sort_key = lambda item: (item.modified_at, item.filename.casefold(), item.filename)
        elif key == "type":
            sort_key = lambda item: (item.extension.casefold(), item.filename.casefold(), item.filename)
        else:
            sort_key = lambda item: (item.filename.casefold(), item.filename)
        return sorted(folders, key=sort_key, reverse=self._sort_descending) + sorted(
            files, key=sort_key, reverse=self._sort_descending
        )

    def _sort_changed(self, _index: int) -> None:
        self._sort_key = self._sort_combo.currentData()
        if self._search_query.strip():
            self._render_items(self._search_results, search=True)
        elif self._listing is not None:
            self._render_items(self._listing.items)

    def _toggle_sort_order(self) -> None:
        self._sort_descending = not self._sort_descending
        self._sort_order_button.setText("Descending" if self._sort_descending else "Ascending")
        self._sort_changed(self._sort_combo.currentIndex())

    def _render_breadcrumbs(self) -> None:
        while self._breadcrumbs.count():
            item = self._breadcrumbs.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        root = QPushButton("Shared folder")
        root.setFlat(True)
        root.clicked.connect(lambda _checked=False: self._navigate_to(""))
        self._breadcrumbs.addWidget(root)
        partial = []
        for segment in self._path.split("/") if self._path else ():
            partial.append(segment)
            separator = QLabel("›")
            self._breadcrumbs.addWidget(separator)
            target = "/".join(partial)
            crumb = QPushButton(segment)
            crumb.setFlat(True)
            crumb.clicked.connect(lambda _checked=False, path=target: self._navigate_to(path))
            self._breadcrumbs.addWidget(crumb)
        self._breadcrumbs.addStretch(1)
        self._path_label.setText(f"Current folder: /{self._path}" if self._path else "Current folder: /")

    def _navigate_to(self, path: str) -> None:
        self._search_timer.stop()
        if self._search_input.text():
            self._search_input.blockSignals(True)
            self._search_input.clear()
            self._search_input.blockSignals(False)
        self._search_query = ""
        self._search_results = []
        self._open_location_button.setEnabled(False)
        self._path = path
        self._render_breadcrumbs()
        self._load_directory()

    def _open_search_result_location(self) -> None:
        row = self._items.currentItem()
        if row is None:
            return
        item: FileItem = row.data(Qt.ItemDataRole.UserRole)
        self._navigate_to(item.relative_path.rpartition("/")[0])

    def _open_transfer_history(self) -> None:
        if self._store is not None:
            HistoryDialog(self._store, "transfers", self).exec()

    def _load_directory(self) -> None:
        if self._busy or self._session_unavailable:
            return
        self._busy = True
        self._status.setText("Loading shared files…")
        self._empty_state.hide()

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
        if self._state.permission is not None:
            self._permission = self._state.permission
        listing: DirectoryListing = self._listing
        self._path = listing.directory_path
        self._render_breadcrumbs()
        if self._search_query.strip():
            if not self._search_busy:
                self._render_items(self._search_results, search=True)
        else:
            self._render_items(listing.items)
        if not self._state.available:
            self._items.hide()
            self._empty_state.setText("The host's shared folder is unavailable. Ask the host to select an accessible folder.")
            self._empty_state.show()
            self._status.setText(
                f"Permission: {self._permission.value}. The host is not currently sharing a folder."
            )
        elif self._permission is PermissionLevel.READ_ONLY:
            self._status.setText(
                f"Browsing {listing.shared_directory_name}. This device has READ_ONLY permission."
            )
        elif self._state.uploads_enabled:
            self._status.setText(
                f"Browsing {listing.shared_directory_name}. Permission: READ_WRITE. Uploads are enabled."
            )
        else:
            self._status.setText(
                f"Browsing {listing.shared_directory_name}. Permission: READ_WRITE. "
                "The host has uploads disabled."
            )
        self._update_actions()
        self._live_refresh_attempts = 0
        if self._live_refresh_pending:
            self._live_refresh_timer.start()
        if self._search_pending:
            self._search_timer.start(0)

    def apply_permission(self, permission: PermissionLevel) -> None:
        """Apply a permission change received on the live session connection."""
        self._permission = permission
        self._update_status_for_permission()
        self._update_actions()

    def set_session_unavailable(self, detail: str) -> None:
        """Disable file actions after session access or TLS trust is lost."""
        self._session_unavailable = detail
        for cancel_event in self._cancel_events.values():
            cancel_event.set()
        self._status.setText(detail)
        self._update_actions()

    def set_session_available(self) -> None:
        """Restore API access after the verified event connection is authenticated again."""
        self._session_unavailable = None
        self._status.setText("Connection restored. Refreshing the shared folder…")
        self.recover_authoritative()
        self._update_actions()

    def apply_sharing_state(
        self, *, available: bool, uploads_enabled: bool, directory_name: str | None
    ) -> None:
        """Apply host file-sharing settings delivered through the session event stream."""
        previous_name = self._state.directory_name if self._state else None
        if self._state is not None:
            self._state = self._state.model_copy(update={
                "available": available,
                "shared": available,
                "uploads_enabled": uploads_enabled and available,
                "directory_name": directory_name,
            })
        if not available:
            self._path = ""
            self._listing = None
            self._search_results = []
            self._render_items([], search=bool(self._search_query.strip()))
            self._empty_state.setText("The host is not currently sharing an available folder.")
            self._status.setText(
                f"Permission: {self._permission.value}. The host is not currently sharing a folder."
            )
        elif directory_name != previous_name:
            self._path = ""
            if self._search_query.strip():
                self._search_results = []
                self._search_pending = True
                self._search_timer.start(0)
            else:
                self._load_directory()
        else:
            self._update_status_for_permission()
        self._update_actions()

    def handle_session_event(self, event: dict[str, object]) -> None:
        """Treat authenticated events as hints and refresh via the file API."""
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if not isinstance(data, dict):
            data = event
        event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
        if event_type in {"file_created", "file_modified", "file_deleted"}:
            relative = data.get("relative_path")
            parent = data.get("parent_path", "")
            filename = data.get("filename", "shared file")
            if not isinstance(relative, str) or not isinstance(parent, str):
                return
            if self._search_query.strip():
                needle = self._search_query.strip().casefold()
                matches_existing = any(
                    item.relative_path == relative
                    or item.relative_path.startswith(relative.rstrip("/") + "/")
                    for item in self._search_results
                )
                if needle in filename.casefold() or matches_existing:
                    self._notifications.notify(
                        "Shared files changed", str(filename), event_id=event_id,
                        category=event_type,
                    )
                    self._queue_live_refresh()
            elif parent == self._path:
                title = {
                    "file_created": "New shared file",
                    "file_modified": "Shared file updated",
                    "file_deleted": "Shared file removed",
                }[event_type]
                self._notifications.notify(title, str(filename), event_id=event_id, category=event_type)
                self._queue_live_refresh()
            elif event_type == "file_deleted" and self._path.startswith(relative + "/"):
                self._path = parent
                self._queue_live_refresh()
            return
        if event_type == "shared_directory_unavailable":
            self.apply_sharing_state(
                available=False,
                uploads_enabled=False,
                directory_name=data.get("directory_name") if isinstance(data.get("directory_name"), str) else None,
            )
        elif event_type == "shared_directory_changed":
            available = bool(data.get("available"))
            directory_name = data.get("directory_name")
            relative = data.get("relative_path")
            if isinstance(relative, str):
                change = data.get("change")
                parent = relative.rpartition("/")[0]
                if self._search_query.strip():
                    needle = self._search_query.strip().casefold()
                    relevant = needle in relative.rpartition("/")[2].casefold() or any(
                        item.relative_path == relative
                        or item.relative_path.startswith(relative.rstrip("/") + "/")
                        for item in self._search_results
                    )
                    if relevant:
                        self._queue_live_refresh()
                if change == "deleted" and (self._path == relative or self._path.startswith(relative + "/")):
                    self._path = parent
                    self._queue_live_refresh()
                elif not self._search_query.strip() and self._path == parent:
                    self._queue_live_refresh()
                return
            previous_name = self._state.directory_name if self._state else None
            was_busy = self._busy
            self._path = ""
            self.apply_sharing_state(
                available=available,
                uploads_enabled=bool(self._state and self._state.uploads_enabled),
                directory_name=directory_name if isinstance(directory_name, str) else None,
            )
            if available and (directory_name == previous_name or was_busy):
                self._queue_live_refresh()
        elif event_type == "file_sharing_state_changed":
            self.apply_sharing_state(
                available=bool(data.get("available")),
                uploads_enabled=bool(data.get("uploads_enabled")),
                directory_name=data.get("directory_name") if isinstance(data.get("directory_name"), str) else None,
            )
        elif event_type == "session_resync_required":
            self.recover_authoritative()
        elif event_type in {"participant_connected", "participant_disconnected"}:
            participant_id = data.get("participant_id")
            owner = self.parent()
            if participant_id == getattr(owner, "_participant_id", None):
                return
            name = data.get("device_name")
            if isinstance(name, str):
                title = "Participant joined" if event_type == "participant_connected" else "Participant left"
                self._notifications.notify(
                    title, name, event_id=event_id, category="participant_activity"
                )

    def recover_authoritative(self) -> None:
        """Reload the server-owned listing after a reconnect or event gap."""
        self._queue_live_refresh()

    def _queue_live_refresh(self) -> None:
        if self._session_unavailable:
            return
        self._live_refresh_pending = True
        self._live_refresh_attempts = 0
        self._live_refresh_timer.start()

    def _run_live_refresh(self) -> None:
        if not self._live_refresh_pending or self._session_unavailable:
            return
        if self._busy:
            return
        self._live_refresh_pending = False
        if self._search_query.strip():
            self._search_pending = True
            self._search_timer.start(0)
        else:
            self._load_directory()

    def _update_status_for_permission(self) -> None:
        if not self._state:
            return
        if not self._state.available:
            self._status.setText(
                f"Permission: {self._permission.value}. The host is not currently sharing a folder."
            )
            return
        if not self._listing:
            return
        if self._permission is PermissionLevel.READ_ONLY:
            self._status.setText(
                f"Browsing {self._listing.shared_directory_name}. This device has READ_ONLY permission."
            )
        elif self._state.uploads_enabled:
            self._status.setText(
                f"Browsing {self._listing.shared_directory_name}. Permission: READ_WRITE. "
                "Uploads are enabled."
            )
        else:
            self._status.setText(
                f"Browsing {self._listing.shared_directory_name}. Permission: READ_WRITE. "
                "The host has uploads disabled."
            )

    def _operation_failed(self, message: str) -> None:
        self._busy = False
        if self._operation_id:
            self._cancel_events.pop(self._operation_id, None)
        self._operation_id = None
        if "certificate changed" in message.casefold() or "certificate validation" in message.casefold():
            self._session_unavailable = message
            self._status.setText(message)
        else:
            self._status.setText(f"File operation failed: {message}")
        self._update_actions()
        if self._live_refresh_pending and not self._session_unavailable:
            self._live_refresh_timer.start()
        elif (
            self._live_refresh_attempts == 0
            and self._path
            and any(marker in message.casefold() for marker in ("not found", "not a directory", "directory is unavailable"))
        ):
            self._live_refresh_attempts = 1
            self._path = self._path.rpartition("/")[0]
            self._live_refresh_pending = True
            self._live_refresh_timer.start()

    def _go_up(self) -> None:
        if self._listing is None or self._listing.parent_path is None:
            return
        self._navigate_to(self._listing.parent_path)

    def _activate_item(self, row: QListWidgetItem) -> None:
        item: FileItem = row.data(Qt.ItemDataRole.UserRole)
        if self._search_query.strip():
            self._open_search_result_location()
            return
        if item.item_type is FileItemType.DIRECTORY and item.accessible:
            self._navigate_to(item.relative_path)
        elif item.item_type is FileItemType.FILE and item.accessible:
            self._download_selected()

    def _download_selected(self) -> None:
        if self._session_unavailable:
            return
        row = self._items.currentItem()
        if row is None:
            return
        item: FileItem = row.data(Qt.ItemDataRole.UserRole)
        if item.item_type is not FileItemType.FILE or not item.accessible:
            return
        download_directory = Path(str(self._preferences.get("download_directory", ""))).expanduser()
        initial_destination = str(download_directory / item.filename) if download_directory.is_dir() else item.filename
        destination, _ = QFileDialog.getSaveFileName(
            self, "Save shared file", initial_destination
        )
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
        if self._session_unavailable:
            return
        if self._permission is not PermissionLevel.READ_WRITE:
            self._status.setText("This device has READ_ONLY permission. Ask the host for upload access.")
            return
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
        if self._session_unavailable:
            return
        if len(self._active_transfer_ids) >= 3:
            self._status.setText("Three transfers are already active. Wait for one to finish before starting another.")
            return
        transfer_id = uuid.uuid4().hex
        cancel_event = threading.Event()
        self._cancel_events[transfer_id] = cancel_event
        self._active_transfer_ids.add(transfer_id)
        self._status.setText(f"Starting transfer: {name}")
        worker = None

        def run():
            return operation(transfer_id, cancel_event, worker)

        worker = SessionWorker(run)
        worker.signals.progress.connect(self._transfer_progress)
        worker.signals.finished.connect(
            lambda result, identifier=transfer_id: self._transfer_finished(result, refresh_after, identifier)
        )
        worker.signals.failed.connect(
            lambda message, identifier=transfer_id: self._transfer_failed(message, identifier)
        )
        self._thread_pool.start(worker)
        self._update_actions()

    def _transfer_progress(self, transfer: object) -> None:
        self._status.setText(
            f"{transfer.direction.value.title()} {transfer.filename}: "
            f"{transfer.transferred_bytes:,}/{transfer.total_bytes:,} bytes "
            f"({transfer.progress_percent:.0f}%)"
        )
        self._refresh_transfers()

    def _transfer_finished(self, transfer: object, refresh_after: bool, transfer_id: str) -> None:
        self._cancel_events.pop(transfer_id, None)
        self._active_transfer_ids.discard(transfer_id)
        self._status.setText(f"{transfer.direction.value.title()} of {transfer.filename} {transfer.status.value.lower()}.")
        if transfer.status is TransferStatus.COMPLETED:
            self._notifications.notify(
                "Transfer completed", transfer.filename, category="transfer_completion"
            )
        elif transfer.status is TransferStatus.FAILED:
            self._notifications.notify(
                "Transfer failed", transfer.filename, category="transfer_failure"
            )
        self._refresh_transfers()
        self._update_actions()
        if refresh_after and transfer.status is TransferStatus.COMPLETED:
            self._load_directory()
        elif self._live_refresh_pending:
            self._live_refresh_timer.start()

    def _transfer_failed(self, message: str, transfer_id: str) -> None:
        self._cancel_events.pop(transfer_id, None)
        self._active_transfer_ids.discard(transfer_id)
        try:
            transfer = self.client.transfers.get(transfer_id)
        except LookupError:
            transfer = None
        if transfer is not None and transfer.status is TransferStatus.CANCELLED:
            self._status.setText(f"Transfer cancelled: {transfer.filename}.")
        else:
            filename = transfer.filename if transfer is not None else "The file"
            self._status.setText(f"{filename} could not be transferred: {message}")
            self._notifications.notify(
                "Transfer failed", filename, category="transfer_failure"
            )
        if "certificate changed" in message.casefold() or "certificate validation" in message.casefold():
            self._session_unavailable = message
        self._refresh_transfers()
        self._update_actions()

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
            not self._busy and self._session_unavailable is None and item is not None
            and len(self._active_transfer_ids) < 3
            and self._state is not None and self._state.available
            and item.item_type is FileItemType.FILE and item.accessible
        )
        self._upload_button.setEnabled(
            not self._busy
            and len(self._active_transfer_ids) < 3
            and self._session_unavailable is None
            and self._permission is PermissionLevel.READ_WRITE
            and self._state is not None
            and self._state.uploads_enabled
        )
        self._refresh_button.setEnabled(not self._busy and self._session_unavailable is None)
        self._search_input.setEnabled(not self._busy and self._session_unavailable is None)
        self._clear_search_button.setEnabled(bool(self._search_input.text()))
        selected_row = self._items.currentItem()
        self._open_location_button.setEnabled(
            bool(self._search_query.strip()) and selected_row is not None and not self._busy
        )
        self._back_button.setEnabled(
            not self._busy and self._session_unavailable is None
            and self._listing is not None and self._listing.parent_path is not None
        )
        transfer = self._transfers.currentItem()
        self._cancel_button.setEnabled(
            transfer is not None and bool(transfer.data(Qt.ItemDataRole.UserRole + 1))
        )

    def closeEvent(self, event) -> None:
        if self._busy or self._search_busy or self._active_transfer_ids:
            self._status.setText(
                "Wait for the current operation or cancel its transfer before closing."
            )
            event.ignore()
            return
        self._transfer_timer.stop()
        self._live_refresh_timer.stop()
        self._search_timer.stop()
        self._notifications.close()
        self.client.close()
        super().closeEvent(event)
