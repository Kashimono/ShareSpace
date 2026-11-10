"""Host session dashboard with approval and participant controls."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharespace.core.device_manager import DeviceManager
from sharespace.core.models import (
    HostSnapshot,
    ParticipantRole,
    ParticipantStatus,
    PermissionLevel,
    PublicSession,
    TransferStatus,
)
from sharespace.networking.discovery import SessionAdvertiser
from sharespace.networking.session_client import SessionClient
from sharespace.networking.session_server import SessionServer
from sharespace.platform.platform_manager import PlatformManager
from sharespace.ui.notification_manager import NotificationManager
from sharespace.ui.session_workers import SessionEventWorker, SessionWorker


@dataclass
class _HostRuntime:
    server: SessionServer
    advertiser: SessionAdvertiser
    session: PublicSession
    joining_code: str
    host_token: str
    host_addresses: tuple[str, ...] = ()
    discovery_error: str | None = None


class SessionHostDialog(QDialog):
    """Start a local session and manage join requests and connected devices."""

    def __init__(
        self,
        *,
        session_name: str,
        preferred_host_ip: str | None = None,
        hotspot_manager: PlatformManager | None = None,
        stop_hotspot_on_end: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._session_name = session_name.strip()
        self._preferred_host_ip = preferred_host_ip
        self._hotspot_manager = hotspot_manager
        self._stop_hotspot_on_end = stop_hotspot_on_end
        self._runtime: _HostRuntime | None = None
        self._client: SessionClient | None = None
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(4)
        self._stream_stop = threading.Event()
        self._stream_worker: SessionEventWorker | None = None
        self._busy = False
        self._refresh_busy = False
        self._close_after_end = False

        self.setWindowTitle("ShareSpace host session")
        self.setMinimumSize(780, 780)
        self.resize(920, 900)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        heading = QLabel("Host session")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        self._notifications = NotificationManager(self, layout)
        self._status = QLabel("Starting the local session server…")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #4357c5; font-weight: 600;")
        layout.addWidget(self._status)
        self._activity_label = QLabel("No recent session activity.")
        self._activity_label.setWordWrap(True)
        self._activity_label.setStyleSheet("color: #536177;")
        layout.addWidget(self._activity_label)

        details = QFormLayout()
        self._name_label = QLabel(self._session_name or "ShareSpace session")
        self._id_label = QLabel("Starting…")
        self._code_input = QLineEdit()
        self._code_input.setReadOnly(True)
        self._copy_code_button = QPushButton("Copy joining code")
        self._copy_code_button.clicked.connect(lambda _checked=False: self._copy_code())
        code_row = QHBoxLayout()
        code_row.addWidget(self._code_input, 1)
        code_row.addWidget(self._copy_code_button)
        code_widget = QWidget()
        code_widget.setLayout(code_row)
        self._address_label = QLabel("Selecting a local address…")
        self._address_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._count_label = QLabel("0 connected")
        details.addRow("Session name", self._name_label)
        details.addRow("Session ID", self._id_label)
        details.addRow("Joining code", code_widget)
        details.addRow("Host address", self._address_label)
        details.addRow("Participants", self._count_label)
        layout.addLayout(details)

        security = QFormLayout()
        self._security_status = QLabel("TLS certificate created; local certificate verification is pending.")
        self._security_status.setWordWrap(True)
        self._fingerprint_label = QLabel("Starting…")
        self._fingerprint_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        fingerprint_row = QHBoxLayout()
        fingerprint_row.addWidget(self._fingerprint_label, 1)
        self._copy_fingerprint_button = QPushButton("Copy fingerprint")
        self._copy_fingerprint_button.clicked.connect(lambda _checked=False: self._copy_fingerprint())
        fingerprint_row.addWidget(self._copy_fingerprint_button)
        fingerprint_widget = QWidget()
        fingerprint_widget.setLayout(fingerprint_row)
        security.addRow("Connection security", self._security_status)
        security.addRow("SHA-256 fingerprint", fingerprint_widget)
        layout.addLayout(security)

        sharing = QFormLayout()
        self._shared_path = QLabel("No folder selected")
        self._shared_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._shared_path.setWordWrap(True)
        folder_actions = QHBoxLayout()
        self._select_folder_button = QPushButton("Select shared folder")
        self._select_folder_button.clicked.connect(lambda _checked=False: self._choose_shared_folder())
        self._stop_sharing_button = QPushButton("Stop sharing")
        self._stop_sharing_button.clicked.connect(lambda _checked=False: self._stop_sharing())
        folder_actions.addWidget(self._select_folder_button)
        folder_actions.addWidget(self._stop_sharing_button)
        folder_widget = QWidget()
        folder_widget.setLayout(folder_actions)
        self._uploads_checkbox = QCheckBox("Allow participants to upload files (off by default)")
        self._uploads_checkbox.toggled.connect(self._toggle_uploads)
        self._file_status = QLabel("Choose a folder to share files with connected participants.")
        self._file_status.setWordWrap(True)
        sharing.addRow("Shared folder", self._shared_path)
        sharing.addRow("", folder_widget)
        sharing.addRow("", self._uploads_checkbox)
        sharing.addRow("File sharing", self._file_status)
        layout.addLayout(sharing)

        transfer_header = QHBoxLayout()
        transfer_header.addWidget(QLabel("File transfers"))
        transfer_header.addStretch(1)
        self._cancel_transfer_button = QPushButton("Cancel selected transfer")
        self._cancel_transfer_button.clicked.connect(lambda _checked=False: self._cancel_transfer())
        transfer_header.addWidget(self._cancel_transfer_button)
        layout.addLayout(transfer_header)
        self._transfers = QListWidget()
        self._transfers.setMaximumHeight(110)
        self._transfers.currentItemChanged.connect(lambda _current, _previous: self._update_actions())
        layout.addWidget(self._transfers)

        lists = QHBoxLayout()
        pending_column = QVBoxLayout()
        pending_column.addWidget(QLabel("Pending join requests"))
        self._requests = QListWidget()
        self._requests.currentItemChanged.connect(lambda _current, _previous: self._update_actions())
        pending_column.addWidget(self._requests, 1)
        request_actions = QHBoxLayout()
        self._approve_button = QPushButton("Approve")
        self._approve_button.clicked.connect(lambda _checked=False: self._resolve_request(True))
        self._reject_button = QPushButton("Reject")
        self._reject_button.clicked.connect(lambda _checked=False: self._resolve_request(False))
        request_actions.addWidget(self._approve_button)
        request_actions.addWidget(self._reject_button)
        pending_column.addLayout(request_actions)

        participant_column = QVBoxLayout()
        participant_column.addWidget(QLabel("Participants"))
        self._participants = QListWidget()
        self._participants.currentItemChanged.connect(
            lambda _current, _previous: self._update_actions()
        )
        participant_column.addWidget(self._participants, 1)
        permission_actions = QHBoxLayout()
        self._grant_write_button = QPushButton("Grant READ_WRITE")
        self._grant_write_button.clicked.connect(
            lambda _checked=False: self._change_permission(PermissionLevel.READ_WRITE)
        )
        self._revoke_write_button = QPushButton("Revoke READ_WRITE")
        self._revoke_write_button.clicked.connect(
            lambda _checked=False: self._change_permission(PermissionLevel.READ_ONLY)
        )
        permission_actions.addWidget(self._grant_write_button)
        permission_actions.addWidget(self._revoke_write_button)
        participant_column.addLayout(permission_actions)
        self._disconnect_button = QPushButton("Disconnect participant")
        self._disconnect_button.clicked.connect(lambda _checked=False: self._disconnect_participant())
        participant_column.addWidget(self._disconnect_button)
        lists.addLayout(pending_column, 1)
        lists.addLayout(participant_column, 1)
        layout.addLayout(lists, 1)

        footer = QHBoxLayout()
        self._end_button = QPushButton("End Session")
        self._end_button.setStyleSheet("background: #b42318; color: white; font-weight: 650;")
        self._end_button.clicked.connect(lambda _checked=False: self._confirm_end())
        footer.addStretch(1)
        footer.addWidget(self._end_button)
        layout.addLayout(footer)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(1500)
        self._refresh_timer.timeout.connect(self._refresh_snapshot)
        self._file_timer = QTimer(self)
        self._file_timer.setInterval(750)
        self._file_timer.timeout.connect(self._refresh_file_state)
        QTimer.singleShot(0, self._start_session)
        self._update_actions()

    def _start_session(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._update_actions()

        def start() -> _HostRuntime:
            device_manager = DeviceManager()
            address = device_manager.select_host_address(self._preferred_host_ip)
            if address is None:
                raise RuntimeError("No usable local IPv4 address is available on this device.")
            addresses = [address.address] + [
                item.address
                for item in device_manager.get_local_addresses()
                if item.address != address.address
            ]
            server = SessionServer()
            session, joining_code, host_token = server.start(
                session_name=self._session_name or "ShareSpace session",
                host_device_name=device_manager.hostname,
                host_os=device_manager.operating_system,
                host_ip=address.address,
                host_addresses=tuple(addresses),
            )
            advertiser = SessionAdvertiser(server.manager, device_manager)
            discovery_error = None
            try:
                advertiser.start()
            except Exception as error:
                discovery_error = str(error)
            return _HostRuntime(
                server,
                advertiser,
                session,
                joining_code,
                host_token,
                tuple(addresses),
                discovery_error,
            )

        worker = SessionWorker(start)
        worker.signals.finished.connect(self._session_started)
        worker.signals.failed.connect(self._session_start_failed)
        self._thread_pool.start(worker)

    def _session_started(self, result: object) -> None:
        self._busy = False
        self._runtime = result
        runtime = self._runtime
        if runtime is None:
            return
        self._client = SessionClient(
            runtime.session.host_ip,
            runtime.session.port,
            runtime.session.certificate_fingerprint or "",
        )
        self._name_label.setText(runtime.session.session_name)
        self._id_label.setText(runtime.session.session_id)
        self._fingerprint_label.setText(runtime.session.certificate_fingerprint or "Unavailable")
        self._code_input.setText(runtime.joining_code)
        addresses = [
            f"{address}:{runtime.session.port}" for address in runtime.host_addresses
        ]
        if not addresses:
            addresses = [f"{runtime.session.host_ip}:{runtime.session.port}"]
        address_text = "\n".join(addresses)
        self._address_label.setText(address_text)
        if runtime.discovery_error:
            self._status.setText(
                "Session active. Automatic discovery is unavailable; share the address above "
                "and joining code manually."
            )
            self._status.setStyleSheet("color: #9a6700; font-weight: 600;")
        else:
            self._status.setText("Session active and advertised on the local network.")
            self._status.setStyleSheet("color: #157347; font-weight: 600;")
        self._start_event_stream()
        self._refresh_timer.start()
        self._file_timer.start()
        self._refresh_snapshot()
        self._refresh_file_state()
        self._update_actions()

    def _session_start_failed(self, message: str) -> None:
        self._busy = False
        self._status.setText(f"Could not start the session: {message}")
        self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._end_button.setText("Close")
        self._end_button.clicked.disconnect()
        self._end_button.clicked.connect(lambda _checked=False: self.reject())
        self._update_actions()

    def _start_event_stream(self) -> None:
        runtime = self._runtime
        if runtime is None or self._client is None:
            return
        self._stream_stop.clear()
        worker = SessionEventWorker(
            lambda signals: self._client.events(
                session_id=runtime.session.session_id,
                token=runtime.host_token,
                stop_event=self._stream_stop,
                on_event=signals.event.emit,
                participant=False,
                on_connection_state=signals.connection_state.emit,
            )
        )
        worker.signals.event.connect(self._on_session_event)
        worker.signals.connection_state.connect(self._on_stream_state)
        self._stream_worker = worker
        self._thread_pool.start(worker)

    def _on_session_event(self, event: object) -> None:
        if not isinstance(event, dict):
            return
        if event.get("type") == "session_security_error":
            self._security_status.setText(
                event.get("detail", "The host certificate could not be verified.")
            )
            self._security_status.setStyleSheet("color: #b42318; font-weight: 600;")
            self._status.setText("Secure event connection failed. Review the certificate state.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        event_type = event.get("type")
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if not isinstance(data, dict):
            data = event
        event_id = event.get("event_id") if isinstance(event.get("event_id"), str) else None
        if event_type in {"file_created", "file_modified", "file_deleted"}:
            filename = data.get("filename", "shared file")
            label, title = {
                "file_created": ("File added", "Shared file added"),
                "file_modified": ("File updated", "Shared file updated"),
                "file_deleted": ("File removed", "Shared file removed"),
            }[event_type]
            self._activity_label.setText(f"{label}: {filename}")
            self._notifications.notify(title, str(filename), event_id=event_id)
        elif event_type in {"participant_connected", "participant_disconnected"}:
            name = data.get("device_name", "A participant")
            joined = event_type == "participant_connected"
            self._activity_label.setText(f"{name} {'joined' if joined else 'left'} the session.")
            self._notifications.notify(
                "Participant joined" if joined else "Participant left",
                str(name),
                event_id=event_id,
            )
        elif event_type == "shared_directory_unavailable":
            self._activity_label.setText("The shared directory is unavailable.")
            self._refresh_file_state()
        elif event_type == "shared_directory_changed":
            self._activity_label.setText("The shared directory changed.")
            self._refresh_file_state()
        elif event_type in {"file_sharing_state_changed", "session_resync_required"}:
            self._refresh_file_state()
        self._refresh_snapshot()

    def _on_stream_state(self, state: str) -> None:
        if state == "security_error" and self._runtime is not None:
            self._security_status.setText("TLS certificate validation failed for the live session connection.")
            self._security_status.setStyleSheet("color: #b42318; font-weight: 600;")
        elif state == "disconnected" and self._runtime is not None:
            self._status.setText("Session is active; reconnecting to live updates…")
        elif state == "connected" and self._runtime is not None:
            if self._runtime.discovery_error:
                self._status.setText("Session active; automatic discovery is unavailable.")
            else:
                self._status.setText("Session active and advertised on the local network.")

    def _refresh_snapshot(self) -> None:
        if self._refresh_busy or self._client is None or self._runtime is None:
            return
        self._refresh_busy = True
        worker = SessionWorker(lambda: self._client.host_snapshot(self._runtime.host_token))
        worker.signals.finished.connect(self._snapshot_loaded)
        worker.signals.failed.connect(self._snapshot_failed)
        self._thread_pool.start(worker)

    def _snapshot_loaded(self, result: object) -> None:
        self._refresh_busy = False
        snapshot: HostSnapshot = result
        self._count_label.setText(f"{snapshot.session.participant_count} connected")
        if self._client and self._client.certificate_verified:
            self._security_status.setText(
                f"TLS {self._client.tls_version} active; the host certificate is verified."
            )
            self._security_status.setStyleSheet("color: #157347; font-weight: 600;")
        current_request_id = (
            self._requests.currentItem().data(Qt.ItemDataRole.UserRole)
            if self._requests.currentItem() is not None
            else None
        )
        current_participant_id = (
            self._participants.currentItem().data(Qt.ItemDataRole.UserRole)
            if self._participants.currentItem() is not None
            else None
        )
        self._requests.blockSignals(True)
        self._requests.clear()
        selected_request = None
        for request in snapshot.pending_requests:
            item = QListWidgetItem(
                f"{request.device_name} ({request.operating_system})\nRequested {request.requested_at.astimezone().strftime('%H:%M:%S')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, request.request_id)
            self._requests.addItem(item)
            if request.request_id == current_request_id:
                selected_request = item
        if selected_request is not None:
            self._requests.setCurrentItem(selected_request)
        self._requests.blockSignals(False)
        self._participants.blockSignals(True)
        self._participants.clear()
        selected_participant = None
        for participant in snapshot.participants:
            label = (
                f"{participant.device_name} · {participant.operating_system}\n"
                f"{participant.role.value.title()} · {participant.status.value.title()} · "
                f"{participant.permission.value if participant.permission else 'ADMIN'}"
            )
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, participant.participant_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, participant.role.value)
            item.setData(Qt.ItemDataRole.UserRole + 2, participant.status.value)
            item.setData(
                Qt.ItemDataRole.UserRole + 3,
                participant.permission.value if participant.permission else None,
            )
            self._participants.addItem(item)
            if participant.participant_id == current_participant_id:
                selected_participant = item
        if selected_participant is not None:
            self._participants.setCurrentItem(selected_participant)
        self._participants.blockSignals(False)
        self._update_actions()

    def _snapshot_failed(self, message: str) -> None:
        self._refresh_busy = False
        if self._runtime is not None:
            self._status.setText(f"Session active; could not refresh participant state: {message}")

    def _choose_shared_folder(self) -> None:
        runtime = self._runtime
        if runtime is None or self._busy or self._client is None:
            return
        selected = QFileDialog.getExistingDirectory(self, "Choose a folder to share")
        if not selected:
            return
        self._busy = True
        self._file_status.setText("Opening the selected folder for file sharing…")
        self._update_actions()
        worker = SessionWorker(
            lambda: self._client.select_shared_root(selected, runtime.host_token)
        )
        worker.signals.finished.connect(lambda _result: self._shared_folder_changed())
        worker.signals.failed.connect(self._shared_folder_failed)
        self._thread_pool.start(worker)

    def _shared_folder_changed(self) -> None:
        self._busy = False
        self._refresh_file_state()
        self._update_actions()

    def _shared_folder_failed(self, message: str) -> None:
        self._busy = False
        self._file_status.setText(f"Could not share that folder: {message}")
        self._update_actions()

    def _stop_sharing(self) -> None:
        runtime = self._runtime
        if runtime is None or self._busy or self._client is None:
            return
        self._busy = True
        self._file_status.setText("Stopping file sharing…")
        self._update_actions()
        worker = SessionWorker(lambda: self._client.stop_sharing(runtime.host_token))
        worker.signals.finished.connect(lambda _result: self._shared_folder_changed())
        worker.signals.failed.connect(self._file_control_failed)
        self._thread_pool.start(worker)

    def _toggle_uploads(self, enabled: bool) -> None:
        runtime = self._runtime
        if runtime is None or self._client is None or self._busy:
            return
        self._busy = True
        self._file_status.setText("Updating global upload availability…")
        self._update_actions()
        worker = SessionWorker(
            lambda: self._client.set_uploads_enabled(enabled, runtime.host_token)
        )
        worker.signals.finished.connect(lambda _result: self._shared_folder_changed())
        worker.signals.failed.connect(self._file_control_failed)
        self._thread_pool.start(worker)

    def _file_control_failed(self, message: str) -> None:
        self._busy = False
        self._file_status.setText(f"Could not update file sharing: {message}")
        self._refresh_file_state()
        self._update_actions()

    def _change_permission(self, permission: PermissionLevel) -> None:
        runtime = self._runtime
        item = self._participants.currentItem()
        if runtime is None or item is None or self._client is None or self._busy:
            return
        participant_id = item.data(Qt.ItemDataRole.UserRole)
        device_name = item.text().splitlines()[0]
        if permission is PermissionLevel.READ_WRITE:
            title = "Grant upload access?"
            detail = (
                f"Allow {device_name} to upload new files? They still cannot replace, "
                "modify, rename, or delete shared files."
            )
        else:
            title = "Revoke upload access?"
            detail = f"Stop {device_name} from uploading files now?"
        answer = QMessageBox.question(
            self,
            title,
            detail,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._busy = True
        self._update_actions()
        worker = SessionWorker(
            lambda: self._client.set_participant_permission(
                participant_id, permission.value, runtime.host_token
            )
        )
        worker.signals.finished.connect(self._permission_changed)
        worker.signals.failed.connect(self._action_failed)
        self._thread_pool.start(worker)

    def _permission_changed(self, _result: object) -> None:
        self._busy = False
        self._refresh_snapshot()
        self._update_actions()

    def _copy_fingerprint(self) -> None:
        value = self._fingerprint_label.text()
        if value and value != "Starting…" and value != "Unavailable":
            QApplication.clipboard().setText(value)
            self._security_status.setText("Certificate fingerprint copied. Share it over a trusted channel.")

    def _refresh_file_state(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        service = runtime.server.file_service
        root = service.directories.root
        self._shared_path.setText(str(root) if root is not None else "No folder selected")
        self._uploads_checkbox.blockSignals(True)
        self._uploads_checkbox.setChecked(service.uploads_enabled)
        self._uploads_checkbox.blockSignals(False)
        if root is None:
            self._file_status.setText("File sharing is off. Select a folder to begin.")
        else:
            upload_text = "Uploads are enabled." if service.uploads_enabled else "Uploads are disabled."
            self._file_status.setText(f"Participants can browse and download. {upload_text}")
        current_id = self._transfers.currentItem().data(Qt.ItemDataRole.UserRole) if self._transfers.currentItem() else None
        self._transfers.blockSignals(True)
        self._transfers.clear()
        selected = None
        for transfer in service.transfers.list():
            item = QListWidgetItem(
                f"{transfer.direction.value.title()} · {transfer.filename} · "
                f"{transfer.transferred_bytes:,}/{transfer.total_bytes:,} bytes · "
                f"{transfer.progress_percent:.0f}% · {transfer.status.value.replace('_', ' ').title()}"
            )
            item.setData(Qt.ItemDataRole.UserRole, transfer.transfer_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, transfer.status in {
                TransferStatus.QUEUED, TransferStatus.IN_PROGRESS
            })
            self._transfers.addItem(item)
            if transfer.transfer_id == current_id:
                selected = item
        if selected:
            self._transfers.setCurrentItem(selected)
        self._transfers.blockSignals(False)
        self._update_actions()

    def _cancel_transfer(self) -> None:
        item = self._transfers.currentItem()
        if item is None or self._runtime is None or self._client is None or self._busy:
            return
        transfer_id = item.data(Qt.ItemDataRole.UserRole)
        runtime = self._runtime
        self._run_action(
            lambda: self._client.cancel_host_transfer(transfer_id, runtime.host_token)
        )

    def _resolve_request(self, approve: bool) -> None:
        item = self._requests.currentItem()
        runtime = self._runtime
        if item is None or runtime is None or self._client is None or self._busy:
            return
        request_id = item.data(Qt.ItemDataRole.UserRole)
        operation = (
            self._client.approve_request(request_id, runtime.host_token)
            if approve
            else self._client.reject_request(request_id, runtime.host_token)
        )
        self._run_action(lambda: operation)

    def _disconnect_participant(self) -> None:
        item = self._participants.currentItem()
        runtime = self._runtime
        if item is None or runtime is None or self._client is None or self._busy:
            return
        participant_id = item.data(Qt.ItemDataRole.UserRole)
        self._run_action(
            lambda: self._client.disconnect_participant(participant_id, runtime.host_token)
        )

    def _run_action(self, operation) -> None:
        self._busy = True
        self._update_actions()
        worker = SessionWorker(operation)
        worker.signals.finished.connect(self._action_finished)
        worker.signals.failed.connect(self._action_failed)
        self._thread_pool.start(worker)

    def _action_finished(self, _result: object) -> None:
        self._busy = False
        self._refresh_snapshot()
        self._update_actions()

    def _action_failed(self, message: str) -> None:
        self._busy = False
        QMessageBox.warning(self, "Session action failed", message)
        self._update_actions()

    def _copy_code(self) -> None:
        code = self._code_input.text()
        if code:
            QApplication.clipboard().setText(code)
            self._status.setText("Joining code copied.")

    def _confirm_end(self) -> None:
        if self._runtime is None:
            self.reject()
            return
        answer = QMessageBox.question(
            self,
            "End ShareSpace session?",
            "This will disconnect all participants and stop this session. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._end_session(close_after=True)

    def _end_session(self, *, close_after: bool) -> None:
        runtime = self._runtime
        if runtime is None or self._busy:
            return
        self._busy = True
        self._close_after_end = close_after
        self._stream_stop.set()
        self._refresh_timer.stop()
        self._file_timer.stop()
        self._status.setText("Ending session and releasing local networking resources…")
        self._update_actions()

        def cleanup() -> str:
            failures: list[str] = []
            if self._client is not None:
                try:
                    asyncio.run(self._client.end_session(runtime.host_token))
                except Exception as error:
                    failures.append(f"Session notification failed: {error}")
                    try:
                        runtime.server.manager.end_session(runtime.host_token)
                    except Exception:
                        pass
            try:
                runtime.advertiser.stop()
            except Exception as error:
                failures.append(f"Discovery cleanup failed: {error}")
            try:
                runtime.server.stop()
            except Exception as error:
                failures.append(f"Session server cleanup failed: {error}")
            if self._stop_hotspot_on_end and self._hotspot_manager is not None:
                try:
                    self._hotspot_manager.stop_hotspot()
                except Exception as error:
                    failures.append(f"Hotspot cleanup failed: {error}")
            return "\n".join(failures)

        worker = SessionWorker(cleanup)
        worker.signals.finished.connect(self._end_finished)
        worker.signals.failed.connect(self._end_failed)
        self._thread_pool.start(worker)

    def _end_finished(self, failures: object) -> None:
        self._busy = False
        self._runtime = None
        if self._client is not None:
            self._client.close()
        self._client = None
        if failures:
            QMessageBox.warning(self, "Session ended with cleanup issues", str(failures))
        if self._close_after_end:
            self.accept()
        else:
            self._status.setText("Session ended.")
            self.reject()

    def _end_failed(self, message: str) -> None:
        self._busy = False
        QMessageBox.warning(self, "Session cleanup failed", message)
        if self._close_after_end:
            self.accept()

    def _update_actions(self) -> None:
        available = self._runtime is not None and not self._busy
        self._copy_code_button.setEnabled(available and bool(self._code_input.text()))
        self._end_button.setEnabled(not self._busy)
        request_available = available and self._requests.currentItem() is not None
        self._approve_button.setEnabled(request_available)
        self._reject_button.setEnabled(request_available)
        participant_item = self._participants.currentItem()
        can_disconnect = False
        if available and participant_item is not None:
            can_disconnect = (
                participant_item.data(Qt.ItemDataRole.UserRole + 1) == ParticipantRole.PARTICIPANT.value
                and participant_item.data(Qt.ItemDataRole.UserRole + 2) == ParticipantStatus.CONNECTED.value
            )
        self._disconnect_button.setEnabled(can_disconnect)
        can_change_permission = can_disconnect and participant_item is not None
        permission = participant_item.data(Qt.ItemDataRole.UserRole + 3) if participant_item else None
        self._grant_write_button.setEnabled(
            can_change_permission and permission == PermissionLevel.READ_ONLY.value
        )
        self._revoke_write_button.setEnabled(
            can_change_permission and permission == PermissionLevel.READ_WRITE.value
        )
        self._copy_fingerprint_button.setEnabled(available and bool(self._fingerprint_label.text()))
        sharing_available = self._runtime is not None and not self._busy
        self._select_folder_button.setEnabled(sharing_available)
        root_available = sharing_available and self._runtime.server.file_service.directories.root is not None
        self._stop_sharing_button.setEnabled(root_available)
        self._uploads_checkbox.setEnabled(root_available)
        selected_transfer = self._transfers.currentItem()
        self._cancel_transfer_button.setEnabled(
            sharing_available and selected_transfer is not None
            and bool(selected_transfer.data(Qt.ItemDataRole.UserRole + 1))
        )

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            return
        if self._runtime is not None:
            answer = QMessageBox.question(
                self,
                "End ShareSpace session?",
                "The session is still active. End it before closing this window?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._end_session(close_after=True)
            event.ignore()
            return
        self._stream_stop.set()
        self._file_timer.stop()
        self._notifications.close()
        super().closeEvent(event)
