"""Join nearby sessions or connect to a host address manually."""

from __future__ import annotations

import ipaddress
import threading
import uuid

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sharespace.core.device_manager import DeviceManager
from sharespace.core.models import JoinRequestCreated, JoinRequestStatus, PermissionLevel, PublicSession
from sharespace.networking.discovery import SessionDiscovery
from sharespace.networking.file_client import FileClient
from sharespace.networking.session_client import SessionClient
from sharespace.ui.file_browser_dialog import FileBrowserDialog
from sharespace.ui.notification_manager import NotificationManager
from sharespace.ui.session_workers import SessionEventWorker, SessionWorker


class SessionJoinDialog(QDialog):
    """Discover a session, request access, and monitor approval and connection."""

    discovery_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(4)
        self._discovery = SessionDiscovery(on_change=self.discovery_changed.emit)
        self._selected_session: PublicSession | None = None
        self._endpoint: tuple[str, int] | None = None
        self._manual_selection = False
        self._client: SessionClient | None = None
        self._file_browser: FileBrowserDialog | None = None
        self._permission = PermissionLevel.READ_ONLY
        self._request_id: str | None = None
        self._request_token: str | None = None
        self._participant_token: str | None = None
        self._client_id = uuid.uuid4().hex
        self._participant_id: str | None = None
        self._participant_connected = False
        self._stream_stop = threading.Event()
        self._stream_worker: SessionEventWorker | None = None
        self._discovery_busy = False
        self._join_busy = False
        self._poll_busy = False

        self.setWindowTitle("Join a ShareSpace session")
        self.setMinimumSize(650, 570)
        self.resize(760, 650)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)
        heading = QLabel("Join a session")
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        self._notifications = NotificationManager(self, layout)
        self._status = QLabel("Discovering sessions on the local network…")
        self._status.setWordWrap(True)
        self._status.setStyleSheet("color: #4357c5; font-weight: 600;")
        layout.addWidget(self._status)

        session_header = QHBoxLayout()
        session_header.addWidget(QLabel("Available ShareSpace sessions"))
        session_header.addStretch(1)
        self._refresh_button = QPushButton("Refresh")
        self._refresh_button.clicked.connect(lambda _checked=False: self._refresh_discovery())
        session_header.addWidget(self._refresh_button)
        layout.addLayout(session_header)
        self._sessions = QListWidget()
        self._sessions.currentItemChanged.connect(self._session_selected)
        layout.addWidget(self._sessions, 1)

        manual_form = QFormLayout()
        self._host_input = QLineEdit()
        self._host_input.setPlaceholderText("192.168.1.20")
        self._host_input.textChanged.connect(lambda _text: self._manual_endpoint_changed())
        self._port_input = QLineEdit()
        self._port_input.setPlaceholderText("Host port")
        self._port_input.textChanged.connect(lambda _text: self._manual_endpoint_changed())
        self._fingerprint_input = QLineEdit()
        self._fingerprint_input.setPlaceholderText("64 hex characters, SHA-256")
        self._fingerprint_input.textChanged.connect(lambda _text: self._fingerprint_changed())
        self._trust_confirmation = QCheckBox(
            "I compared this fingerprint with the host through a trusted channel"
        )
        self._trust_confirmation.toggled.connect(lambda _checked: self._update_buttons())
        self._manual_button = QPushButton("Verify host certificate")
        self._manual_button.clicked.connect(lambda _checked=False: self._connect_manually())
        manual_row = QHBoxLayout()
        manual_row.addWidget(self._host_input, 1)
        manual_row.addWidget(self._port_input)
        manual_row.addWidget(self._manual_button)
        manual_widget = QWidget()
        manual_widget.setLayout(manual_row)
        manual_form.addRow("Host IPv4 address and port", manual_widget)
        manual_form.addRow("SHA-256 certificate fingerprint", self._fingerprint_input)
        manual_form.addRow("Trust check", self._trust_confirmation)
        layout.addLayout(manual_form)
        self._security_status = QLabel(
            "Compare the fingerprint with the host using a trusted channel. "
            "Local network discovery alone does not verify host identity."
        )
        self._security_status.setWordWrap(True)
        layout.addWidget(self._security_status)

        join_form = QFormLayout()
        self._code_input = QLineEdit()
        self._code_input.setPlaceholderText("Enter the joining code from the host")
        self._code_input.setMaxLength(32)
        self._code_input.textChanged.connect(lambda _text: self._update_buttons())
        join_form.addRow("Joining code", self._code_input)
        layout.addLayout(join_form)

        action_row = QHBoxLayout()
        self._selected_label = QLabel("Select a session or connect manually.")
        self._selected_label.setWordWrap(True)
        action_row.addWidget(self._selected_label, 1)
        self._join_button = QPushButton("Request to Join")
        self._join_button.clicked.connect(lambda _checked=False: self._request_to_join())
        action_row.addWidget(self._join_button)
        self._browse_button = QPushButton("Browse Shared Files")
        self._browse_button.clicked.connect(lambda _checked=False: self._open_file_browser())
        action_row.addWidget(self._browse_button)
        layout.addLayout(action_row)

        self.discovery_changed.connect(self._update_discovered_sessions)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(1500)
        self._poll_timer.timeout.connect(self._poll_request)
        self._discovery_timer = QTimer(self)
        self._discovery_timer.setInterval(1000)
        self._discovery_timer.timeout.connect(self._update_discovered_sessions)
        QTimer.singleShot(0, self._start_discovery)
        self._update_buttons()

    def _start_discovery(self) -> None:
        if self._discovery_busy:
            return
        self._discovery_busy = True
        self._update_buttons()
        worker = SessionWorker(self._discovery.start)
        worker.signals.finished.connect(self._discovery_started)
        worker.signals.failed.connect(self._discovery_failed)
        self._thread_pool.start(worker)

    def _discovery_started(self, _result: object) -> None:
        self._discovery_busy = False
        self._discovery_timer.start()
        self._update_discovered_sessions()
        self._update_buttons()

    def _discovery_failed(self, message: str) -> None:
        self._discovery_busy = False
        self._status.setText(
            f"Automatic discovery is unavailable ({message}). Enter a local host address and port below."
        )
        self._status.setStyleSheet("color: #9a6700; font-weight: 600;")
        self._update_buttons()

    def _refresh_discovery(self) -> None:
        if self._discovery_busy:
            return
        self._discovery_busy = True
        self._status.setText("Refreshing local session discovery…")
        self._update_buttons()
        operation = self._discovery.refresh if self._discovery.active else self._discovery.start
        worker = SessionWorker(operation)
        worker.signals.finished.connect(self._discovery_started)
        worker.signals.failed.connect(self._discovery_failed)
        self._thread_pool.start(worker)

    def _update_discovered_sessions(self) -> None:
        sessions = self._discovery.sessions()
        current_id = self._selected_session.session_id if self._selected_session else None
        self._sessions.blockSignals(True)
        self._sessions.clear()
        selected_row = -1
        for index, session in enumerate(sessions):
            item = QListWidgetItem(
                f"{session.session_name} — {session.host_device_name}\n"
                f"{session.participant_count} participant(s) · {session.host_ip}:{session.port}"
            )
            item.setData(Qt.ItemDataRole.UserRole, session)
            self._sessions.addItem(item)
            if session.session_id == current_id:
                selected_row = index
        if selected_row >= 0:
            self._sessions.setCurrentRow(selected_row)
            fresh_session = sessions[selected_row]
            if not self._manual_selection and self._client is None:
                address_changed = self._endpoint != (fresh_session.host_ip, fresh_session.port)
                fingerprint_changed = (
                    self._selected_session is not None
                    and self._selected_session.certificate_fingerprint
                    != fresh_session.certificate_fingerprint
                )
                self._endpoint = (fresh_session.host_ip, fresh_session.port)
                if address_changed or fingerprint_changed:
                    for field in (self._host_input, self._port_input, self._fingerprint_input):
                        field.blockSignals(True)
                    self._host_input.setText(fresh_session.host_ip)
                    self._port_input.setText(str(fresh_session.port))
                    self._fingerprint_input.setText(fresh_session.certificate_fingerprint or "")
                    for field in (self._host_input, self._port_input, self._fingerprint_input):
                        field.blockSignals(False)
                    self._trust_confirmation.setChecked(False)
                    if fingerprint_changed:
                        self._security_status.setText(
                            "The advertised certificate fingerprint changed. Compare it again through a trusted channel."
                        )
            self._selected_session = fresh_session
        elif (
            self._selected_session is not None
            and not self._manual_selection
            and self._request_id is None
            and self._participant_token is None
        ):
            self._selected_session = None
            self._endpoint = None
            self._trust_confirmation.setChecked(False)
            self._clear_verified_client()
            self._selected_label.setText("Select a session or connect manually.")
            if self._request_id is None:
                self._status.setText("The selected session is unavailable.")
                self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._sessions.blockSignals(False)
        if sessions:
            if self._status.text().startswith("Discovering") or self._status.text().startswith("Refreshing"):
                self._status.setText("Session available. Enter its joining code to request access.")
                self._status.setStyleSheet("color: #157347; font-weight: 600;")
        elif (
            self._selected_session is None
            and self._request_id is None
            and self._participant_token is None
        ):
            if self._discovery.active:
                self._status.setText("No sessions found yet. You can refresh or connect by host address and port.")
                self._status.setStyleSheet("color: #647087;")
        self._update_buttons()

    def _session_selected(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return
        session = current.data(Qt.ItemDataRole.UserRole)
        if isinstance(session, PublicSession):
            self._selected_session = session
            self._endpoint = (session.host_ip, session.port)
            self._manual_selection = False
            for field in (self._host_input, self._port_input, self._fingerprint_input):
                field.blockSignals(True)
            self._host_input.setText(session.host_ip)
            self._port_input.setText(str(session.port))
            self._fingerprint_input.setText(session.certificate_fingerprint or "")
            for field in (self._host_input, self._port_input, self._fingerprint_input):
                field.blockSignals(False)
            self._trust_confirmation.setChecked(False)
            self._clear_verified_client()
            self._security_status.setText(
                "Compare the advertised fingerprint with the host over a trusted channel, then confirm below."
            )
            self._selected_label.setText(
                f"Selected: {session.session_name} on {session.host_device_name}"
            )
            self._update_buttons()

    def _manual_endpoint_changed(self) -> None:
        self._manual_selection = True
        self._selected_session = None
        self._endpoint = None
        self._trust_confirmation.setChecked(False)
        self._clear_verified_client()
        self._selected_label.setText("Manual host details changed. Verify the host again.")
        self._update_buttons()

    def _fingerprint_changed(self) -> None:
        expected = "".join(
            character for character in self._fingerprint_input.text().lower()
            if character in "0123456789abcdef"
        )
        self._trust_confirmation.setChecked(False)
        if self._client is not None and expected != self._client.security.expected_fingerprint:
            self._clear_verified_client()
        self._update_buttons()

    def _clear_verified_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self._participant_connected = False
        self._update_buttons()

    def _connect_manually(self) -> None:
        if self._join_busy:
            return
        try:
            host_ip = str(ipaddress.ip_address(self._host_input.text().strip()))
            parsed_host = ipaddress.ip_address(host_ip)
            if parsed_host.version != 4:
                raise ValueError("Manual connection currently supports IPv4 addresses.")
            if parsed_host.is_global:
                raise ValueError("Enter an IPv4 address on a local network.")
            port = int(self._port_input.text().strip())
            if not 1 <= port <= 65535:
                raise ValueError("Port must be between 1 and 65535.")
            fingerprint = "".join(
                character for character in self._fingerprint_input.text().lower()
                if character in "0123456789abcdef"
            )
            if len(fingerprint) != 64:
                raise ValueError("Enter the complete 64-character SHA-256 certificate fingerprint.")
            if not self._trust_confirmation.isChecked():
                raise ValueError("Confirm that you compared the fingerprint through a trusted channel.")
        except ValueError as error:
            self._status.setText(str(error) or "Enter a valid local host IPv4 address and port.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            return

        self._join_busy = True
        self._status.setText("Checking the ShareSpace host…")
        self._update_buttons()
        self._clear_verified_client()
        client = SessionClient(host_ip, port, fingerprint)
        worker = SessionWorker(lambda: client.get_public_session())
        worker.signals.finished.connect(
            lambda result: self._manual_session_found(result, host_ip, port, client)
        )
        worker.signals.failed.connect(lambda message: self._manual_session_failed(message, client))
        self._thread_pool.start(worker)

    def _manual_session_found(
        self, result: object, host_ip: str, port: int, client: SessionClient
    ) -> None:
        self._join_busy = False
        self._selected_session = result
        self._endpoint = (host_ip, port)
        self._client = client
        self._selected_label.setText(f"Manual host: {result.session_name} on {result.host_device_name}")
        self._status.setText("Host identity verified. Enter its joining code to request access.")
        self._status.setStyleSheet("color: #157347; font-weight: 600;")
        self._security_status.setText(
            f"TLS {client.tls_version} active. Verified fingerprint: {result.certificate_fingerprint}"
        )
        self._security_status.setStyleSheet("color: #157347; font-weight: 600;")
        self._update_buttons()

    def _manual_session_failed(self, message: str, client: SessionClient) -> None:
        self._join_busy = False
        client.close()
        self._status.setText(f"Connection failed: {message}")
        self._security_status.setText("Host certificate verification did not complete.")
        self._security_status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._update_buttons()

    def _request_to_join(self) -> None:
        session = self._selected_session
        endpoint = self._endpoint
        code = self._code_input.text().strip()
        if session is None or endpoint is None:
            self._status.setText("Select an available session or connect to a host manually first.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            return
        if not code:
            self._status.setText("Enter the joining code provided by the host.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            return
        if self._join_busy or self._request_id is not None:
            return

        self._join_busy = True
        self._status.setText("Sending a join request…")
        self._status.setStyleSheet("color: #4357c5; font-weight: 600;")
        self._update_buttons()
        device = DeviceManager()
        client = self._client
        if client is None or not client.certificate_verified:
            self._join_busy = False
            self._status.setText("Verify the host fingerprint before requesting access.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            self._update_buttons()
            return
        operation = lambda: client.request_join(
            session_id=session.session_id,
            client_id=self._client_id,
            device_name=device.hostname,
            operating_system=device.operating_system,
            joining_code=code,
        )
        worker = SessionWorker(operation)
        worker.signals.finished.connect(lambda result: self._join_request_created(result, client))
        worker.signals.failed.connect(self._join_request_failed)
        self._thread_pool.start(worker)

    def _join_request_created(self, result: object, client: SessionClient) -> None:
        self._join_busy = False
        created: JoinRequestCreated = result
        self._client = client
        self._request_id = created.request_id
        self._request_token = created.request_token
        if created.status is JoinRequestStatus.REJECTED:
            self._status.setText("The host rejected the previous request from this device.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        elif created.status is JoinRequestStatus.APPROVED:
            self._status.setText("Request approved. Connecting to the session…")
            self._poll_timer.start()
        else:
            self._status.setText("Request pending. Waiting for the host to review it…")
            self._poll_timer.start()
        self._update_buttons()

    def _join_request_failed(self, message: str) -> None:
        self._join_busy = False
        self._status.setText(f"Connection failed: {message}")
        self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._update_buttons()

    def _poll_request(self) -> None:
        if self._poll_busy or not self._client or not self._request_id or not self._request_token:
            return
        if self._participant_token:
            self._poll_timer.stop()
            return
        self._poll_busy = True
        worker = SessionWorker(
            lambda: self._client.poll_join_request(self._request_id, self._request_token)
        )
        worker.signals.finished.connect(self._request_status_received)
        worker.signals.failed.connect(self._request_poll_failed)
        self._thread_pool.start(worker)

    def _request_status_received(self, result: object) -> None:
        self._poll_busy = False
        status = result.status
        if status is JoinRequestStatus.PENDING:
            self._status.setText("Request pending. Waiting for the host to review it…")
        elif status is JoinRequestStatus.APPROVED:
            if result.participant_token:
                self._participant_token = result.participant_token
                self._poll_timer.stop()
                self._status.setText("Request approved. Connecting to the session…")
                self._start_event_stream()
            else:
                self._status.setText("Request approved. Finishing connection setup…")
        elif status is JoinRequestStatus.REJECTED:
            self._poll_timer.stop()
            self._status.setText("The host rejected your join request.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        else:
            self._poll_timer.stop()
            self._status.setText("The join request expired. Submit a new request to try again.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
        self._update_buttons()

    def _request_poll_failed(self, message: str) -> None:
        self._poll_busy = False
        self._status.setText(f"Connection failed while checking the request: {message}")
        self._status.setStyleSheet("color: #b42318; font-weight: 600;")

    def _start_event_stream(self) -> None:
        if not self._client or not self._selected_session or not self._participant_token:
            return
        if self._stream_worker is not None:
            self._stream_stop.set()
        self._stream_stop = threading.Event()
        client = self._client
        worker = SessionEventWorker(
            lambda signals: client.events(
                session_id=self._selected_session.session_id,
                token=self._participant_token,
                stop_event=self._stream_stop,
                on_event=signals.event.emit,
                participant=True,
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
        if self._file_browser is not None and event.get("type") in {
            "file_created", "file_modified", "file_deleted", "shared_directory_changed",
            "shared_directory_unavailable", "session_resync_required",
            "participant_connected", "participant_disconnected",
        }:
            self._file_browser.handle_session_event(event)
        if event.get("type") == "authenticated":
            self._participant_id = event.get("participant_id")
            self._permission = PermissionLevel(
                event.get("permission", PermissionLevel.READ_ONLY.value)
            )
            self._participant_connected = True
            if self._file_browser is not None:
                self._file_browser.apply_permission(self._permission)
            self._update_buttons()
        elif event.get("type") == "participant_permission_changed":
            self._permission = PermissionLevel(event["permission"])
            if self._file_browser is not None:
                self._file_browser.apply_permission(self._permission)
            self._update_buttons()
        elif event.get("type") == "file_sharing_state_changed":
            if self._file_browser is not None:
                self._file_browser.apply_sharing_state(
                    available=bool(event.get("available")),
                    uploads_enabled=bool(event.get("uploads_enabled")),
                    directory_name=event.get("directory_name"),
                )
        elif event.get("type") in {"participant_connected", "participant_disconnected"}:
            data = event.get("data") if isinstance(event.get("data"), dict) else event
            if (
                event.get("type") == "participant_disconnected"
                and isinstance(data, dict)
                and data.get("participant_id") == self._participant_id
            ):
                self._participant_connected = False
                self._stream_stop.set()
                self._status.setText("The host disconnected this device from the session.")
                self._status.setStyleSheet("color: #b42318; font-weight: 600;")
                if self._file_browser is not None:
                    self._file_browser.set_session_unavailable(
                        "The host disconnected this device from the session."
                    )
            elif isinstance(data, dict) and data.get("participant_id") != self._participant_id:
                name = data.get("device_name")
                if isinstance(name, str) and self._file_browser is None:
                    self._notifications.notify(
                        "Participant joined" if event.get("type") == "participant_connected" else "Participant left",
                        name,
                        event_id=event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                    )
        elif event.get("type") in {
            "file_created", "file_modified", "file_deleted", "shared_directory_changed",
            "shared_directory_unavailable", "session_resync_required",
        }:
            if self._file_browser is None and event.get("type") in {
                "file_created", "file_modified", "file_deleted",
            }:
                data = event.get("data") if isinstance(event.get("data"), dict) else event
                if isinstance(data, dict):
                    title = {
                        "file_created": "New shared file",
                        "file_modified": "Shared file updated",
                        "file_deleted": "Shared file removed",
                    }[event["type"]]
                    filename = data.get("filename", "shared file")
                    self._notifications.notify(
                        title,
                        str(filename),
                        event_id=event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                    )
        elif event.get("type") == "session_ended":
            self._participant_connected = False
            self._poll_timer.stop()
            self._status.setText("The host ended the session.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            if self._file_browser is not None:
                self._file_browser.set_session_unavailable("The host ended the session.")
        elif event.get("type") == "session_security_error":
            self._participant_connected = False
            self._stream_stop.set()
            detail = event.get("detail", "The host certificate could not be verified.")
            presented = event.get("presented_fingerprint")
            if presented:
                detail += f" Presented fingerprint: {presented}"
            self._security_status.setText(detail)
            self._security_status.setStyleSheet("color: #b42318; font-weight: 600;")
            self._status.setText("Secure session connection failed. Verify the host fingerprint again.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            if self._file_browser is not None:
                self._file_browser.set_session_unavailable(detail)
        elif event.get("type") == "error" and event.get("code") == "participant_disconnected":
            self._participant_connected = False
            self._stream_stop.set()
            self._status.setText("The host disconnected this device from the session.")
            self._status.setStyleSheet("color: #b42318; font-weight: 600;")
            if self._file_browser is not None:
                self._file_browser.set_session_unavailable(
                    "The host disconnected this device from the session."
                )
    def _on_stream_state(self, state: str) -> None:
        if state == "connected" and self._participant_token:
            self._participant_connected = True
            tls = self._client.tls_version if self._client else "TLS"
            self._status.setText(f"Connected securely to the ShareSpace session using {tls}.")
            self._status.setStyleSheet("color: #157347; font-weight: 600;")
            if self._file_browser is not None:
                self._file_browser.recover_authoritative()
        elif state == "disconnected" and self._participant_token:
            self._participant_connected = False
            self._status.setText("Session connection interrupted; trying to reconnect…")
            self._status.setStyleSheet("color: #9a6700; font-weight: 600;")
        elif state == "security_error":
            self._participant_connected = False
        self._update_buttons()

    def _open_file_browser(self) -> None:
        if not self._participant_connected or not self._participant_token or self._endpoint is None:
            return
        if self._client is None:
            return
        client = FileClient(
            *self._endpoint, self._participant_token, security=self._client.security
        )
        self._file_browser = FileBrowserDialog(client, self, permission=self._permission)
        self._file_browser.exec()
        self._file_browser = None

    def _update_buttons(self) -> None:
        can_join = (
            self._selected_session is not None
            and self._endpoint is not None
            and self._request_id is None
            and not self._join_busy
            and bool(self._code_input.text().strip())
            and self._client is not None
            and self._client.certificate_verified
            and self._trust_confirmation.isChecked()
            and self._client.security.expected_fingerprint == "".join(
                character for character in self._fingerprint_input.text().lower()
                if character in "0123456789abcdef"
            )
        )
        self._join_button.setEnabled(can_join)
        self._manual_button.setEnabled(not self._join_busy and self._request_id is None)
        self._refresh_button.setEnabled(not self._discovery_busy)
        editable = not self._join_busy and self._request_id is None
        self._sessions.setEnabled(editable)
        self._host_input.setEnabled(editable)
        self._port_input.setEnabled(editable)
        self._fingerprint_input.setEnabled(editable)
        self._trust_confirmation.setEnabled(editable)
        self._code_input.setEnabled(editable)
        self._browse_button.setEnabled(
            self._participant_connected and bool(self._participant_token) and self._endpoint is not None
        )

    def closeEvent(self, event) -> None:
        self._stream_stop.set()
        self._poll_timer.stop()
        self._discovery_timer.stop()
        if self._discovery.active:
            self._thread_pool.start(SessionWorker(self._discovery.stop))
        if self._client is not None:
            self._client.close()
        self._notifications.close()
        super().closeEvent(event)
