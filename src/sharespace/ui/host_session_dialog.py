"""Host Session controls for the Version 0.2 local hotspot milestone."""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from sharespace.platform.base_hotspot import (
    HotspotConfig,
    HotspotError,
    HotspotState,
    NetworkInfo,
    SupportCheck,
    WifiAdapter,
)

logger = logging.getLogger(__name__)


class _WorkerSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class _Worker(QRunnable):
    """Run blocking platform calls without freezing the Qt interface."""

    def __init__(self, function: Callable[[], object]) -> None:
        super().__init__()
        self.function = function
        self.signals = _WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.finished.emit(self.function())
        except Exception as error:
            logger.exception("Hotspot operation failed.")
            self.signals.failed.emit(str(error))


class HostSessionDialog(QDialog):
    """Configure, start, observe, and stop a temporary Wi-Fi hotspot."""

    def __init__(self, platform_manager: object, parent=None) -> None:
        super().__init__(parent)
        self._platform_manager = platform_manager
        self._thread_pool = QThreadPool(self)
        self._thread_pool.setMaxThreadCount(1)
        self._worker: _Worker | None = None
        self._busy = False
        self._active = False
        self._close_after_stop = False
        self._selected_support: SupportCheck | None = None
        self._selected_adapter: WifiAdapter | None = None
        self._last_network_info: NetworkInfo | None = None

        self.setWindowTitle("Host a ShareSpace session")
        self.setMinimumWidth(540)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        heading = QLabel("Create a local Wi-Fi hotspot")
        heading.setStyleSheet("font-size: 21px; font-weight: 650;")
        layout.addWidget(heading)

        intro = QLabel(
            "Create a hotspot with the existing platform adapter, or host over a Wi-Fi or wired "
            "network that is already connected. Linux hotspot availability depends on the radio; "
            "the current unpackaged Windows build cannot create its hotspot."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #647087;")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(12)

        self._ssid_input = QLineEdit("ShareSpace")
        self._ssid_input.setMaxLength(32)
        self._ssid_input.setPlaceholderText("Network name")
        form.addRow("SSID", self._ssid_input)

        password_row = QHBoxLayout()
        self._password_input = QLineEdit()
        self._password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._password_input.setPlaceholderText("8–63 printable characters")
        self._show_password = QCheckBox("Show")
        self._show_password.toggled.connect(self._toggle_password)
        password_row.addWidget(self._password_input, 1)
        password_row.addWidget(self._show_password)
        form.addRow("Wi-Fi password", password_row)

        self._adapter_combo = QComboBox()
        self._adapter_combo.currentIndexChanged.connect(self._adapter_changed)
        form.addRow("Wireless adapter", self._adapter_combo)

        self._session_name_input = QLineEdit("ShareSpace session")
        self._session_name_input.setMaxLength(80)
        self._session_name_input.setPlaceholderText("Session name")
        form.addRow("Session name", self._session_name_input)
        layout.addLayout(form)

        self._compatibility_label = QLabel("Checking hardware compatibility…")
        self._compatibility_label.setWordWrap(True)
        self._compatibility_label.setObjectName("compatibilityStatus")
        layout.addWidget(self._compatibility_label)

        action_row = QHBoxLayout()
        self._check_button = QPushButton("Check Compatibility")
        self._check_button.clicked.connect(lambda _checked=False: self._check_selected_adapter())
        self._create_button = QPushButton("Create Hotspot")
        self._create_button.clicked.connect(lambda _checked=False: self._create_hotspot())
        self._stop_button = QPushButton("Stop Hotspot")
        self._stop_button.clicked.connect(lambda _checked=False: self._stop_hotspot())
        self._stop_button.setEnabled(False)
        action_row.addWidget(self._check_button)
        action_row.addWidget(self._create_button)
        action_row.addWidget(self._stop_button)
        layout.addLayout(action_row)

        session_row = QHBoxLayout()
        self._host_lan_button = QPushButton("Host on this network")
        self._host_lan_button.clicked.connect(lambda _checked=False: self._open_session(False))
        self._host_hotspot_button = QPushButton("Host using this hotspot")
        self._host_hotspot_button.clicked.connect(lambda _checked=False: self._open_session(True))
        self._host_hotspot_button.setEnabled(False)
        session_row.addWidget(self._host_lan_button)
        session_row.addWidget(self._host_hotspot_button)
        layout.addLayout(session_row)

        self._network_info_label = QLabel("Network information appears after the hotspot starts.")
        self._network_info_label.setWordWrap(True)
        self._network_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._network_info_label.setStyleSheet(
            "background: #f5f7fb; border: 1px solid #e1e6ef; border-radius: 8px; "
            "padding: 12px; color: #34405a;"
        )
        layout.addWidget(self._network_info_label)

        self._ssid_input.textChanged.connect(lambda _text: self._update_buttons())
        self._password_input.textChanged.connect(lambda _text: self._update_buttons())

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(2500)
        self._status_timer.timeout.connect(self._poll_status)
        QTimer.singleShot(0, self._load_adapters)

    def _toggle_password(self, show: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        self._password_input.setEchoMode(mode)

    def _set_status(self, text: str, *, kind: str = "info") -> None:
        colors = {
            "info": "#647087",
            "checking": "#4357c5",
            "active": "#157347",
            "stopped": "#647087",
            "failed": "#b42318",
            "ready": "#157347",
        }
        self._compatibility_label.setStyleSheet(
            f"color: {colors.get(kind, colors['info'])}; font-weight: 600;"
        )
        self._compatibility_label.setText(text)

    def _launch_task(
        self,
        function: Callable[[], object],
        *,
        working_text: str,
        on_success: Callable[[object], None],
        on_failure: Callable[[str], None] | None = None,
    ) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_status(working_text, kind="checking")
        self._update_buttons()
        worker = _Worker(function)
        self._worker = worker
        worker.signals.finished.connect(
            lambda result, callback=on_success: self._task_succeeded(callback, result)
        )
        worker.signals.failed.connect(
            lambda message, callback=on_failure: self._task_failed(callback, message)
        )
        self._thread_pool.start(worker)

    def _task_succeeded(self, callback: Callable[[object], None], result: object) -> None:
        self._busy = False
        self._worker = None
        callback(result)
        self._update_buttons()

    def _task_failed(
        self,
        callback: Callable[[str], None] | None,
        message: str,
    ) -> None:
        self._busy = False
        self._worker = None
        if callback is not None:
            callback(message)
        else:
            self._operation_failed(message)
        self._update_buttons()

    def _load_adapters(self) -> None:
        def scan() -> tuple[list[WifiAdapter], SupportCheck | None]:
            adapters = self._platform_manager.detect_adapters()
            if not adapters:
                return adapters, None
            selected = next((item for item in adapters if not item.is_connected), adapters[0])
            return adapters, self._platform_manager.check_support(selected.identifier)

        self._launch_task(
            scan,
            working_text="Checking hardware compatibility…",
            on_success=self._adapters_loaded,
            on_failure=self._compatibility_failed,
        )

    def _adapters_loaded(self, result: object) -> None:
        adapters, support = result
        self._adapters = adapters
        self._adapter_combo.blockSignals(True)
        self._adapter_combo.clear()
        for adapter in adapters:
            status = adapter.state
            if adapter.active_connection:
                status += f" · connected to {adapter.active_connection}"
            self._adapter_combo.addItem(f"{adapter.name} ({status})", adapter.identifier)
        self._adapter_combo.blockSignals(False)

        if not adapters:
            self._selected_adapter = None
            self._selected_support = None
            self._set_status("No wireless adapter was detected.", kind="failed")
            self._update_buttons()
            return

        selected_id = self._adapter_combo.currentData()
        self._selected_adapter = next(
            item for item in adapters if item.identifier == selected_id
        )
        self._selected_support = support
        self._show_support_result(support)
        self._update_buttons()

    def _adapter_changed(self, _index: int) -> None:
        if self._busy or not self._adapter_combo.count() or self._active:
            return
        self._selected_adapter = next(
            (
                item
                for item in getattr(self, "_adapters", [])
                if item.identifier == self._adapter_combo.currentData()
            ),
            None,
        )
        self._selected_support = None
        self._check_selected_adapter()

    def _check_selected_adapter(self) -> None:
        adapter_id = self._adapter_combo.currentData()
        if not adapter_id:
            self._set_status("Select a wireless adapter.", kind="failed")
            return

        def check() -> tuple[list[WifiAdapter], SupportCheck]:
            adapters = self._platform_manager.detect_adapters()
            support = self._platform_manager.check_support(adapter_id)
            return adapters, support

        self._launch_task(
            check,
            working_text="Checking hardware compatibility…",
            on_success=self._support_checked,
            on_failure=self._compatibility_failed,
        )

    def _support_checked(self, result: object) -> None:
        adapters, support = result
        self._adapters = adapters
        adapter_id = self._adapter_combo.currentData()
        self._selected_adapter = next(
            (item for item in adapters if item.identifier == adapter_id),
            None,
        )
        self._selected_support = support
        self._show_support_result(support)
        self._update_buttons()

    def _show_support_result(self, support: SupportCheck | None) -> None:
        if support is None:
            self._set_status("Hardware compatibility has not been checked.", kind="info")
        elif support.supported:
            self._set_status(support.message, kind="ready")
        else:
            self._set_status(f"Hardware check failed: {support.message}", kind="failed")

    def _valid_inputs(self) -> bool:
        ssid = self._ssid_input.text()
        password = self._password_input.text()
        return (
            1 <= len(ssid) <= 32
            and ssid == ssid.strip()
            and ssid.isascii()
            and all(0x20 <= ord(char) <= 0x7E for char in ssid)
            and 8 <= len(password) <= 63
            and password == password.strip()
            and password.isascii()
            and all(0x20 <= ord(char) <= 0x7E for char in password)
        )

    def _update_buttons(self) -> None:
        has_adapter = bool(self._adapter_combo.currentData())
        adapter_checked = (
            self._selected_support is not None
            and self._selected_support.supported
            and self._selected_support.adapter_id == self._adapter_combo.currentData()
        )
        self._check_button.setEnabled(not self._busy and has_adapter and not self._active)
        self._create_button.setEnabled(
            not self._busy and adapter_checked and self._valid_inputs() and not self._active
        )
        self._stop_button.setEnabled(not self._busy and self._active)
        self._adapter_combo.setEnabled(not self._busy and not self._active)
        self._ssid_input.setEnabled(not self._busy and not self._active)
        self._password_input.setEnabled(not self._busy and not self._active)
        self._show_password.setEnabled(not self._busy and not self._active)
        self._session_name_input.setEnabled(not self._busy)
        self._host_lan_button.setEnabled(not self._busy)
        self._host_hotspot_button.setEnabled(not self._busy and self._active)

    def _create_hotspot(self) -> None:
        support = self._selected_support
        adapter = self._selected_adapter
        adapter_id = self._adapter_combo.currentData()
        if support is None or not support.supported or not adapter_id:
            self._set_status("Check hardware compatibility before creating a hotspot.", kind="failed")
            return

        confirm_disconnect = False
        if support.requires_disconnect or support.requires_confirmation:
            connection = adapter.active_connection if adapter else None
            warning_text = support.confirmation_message
            if support.requires_disconnect:
                warning_text = (
                    f"Creating a hotspot on this adapter will disconnect it from "
                    f"{connection or 'its current network'}. ShareSpace will try to restore the "
                    "previous connection when you stop the hotspot. Continue?"
                )
            answer = QMessageBox.warning(
                self,
                "Confirm hotspot networking changes",
                warning_text or "Continue with hotspot creation?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            confirm_disconnect = support.requires_disconnect

        config = HotspotConfig(
            ssid=self._ssid_input.text(),
            password=self._password_input.text(),
            adapter_id=adapter_id,
            confirm_disconnect=confirm_disconnect,
        )
        try:
            from sharespace.platform.base_hotspot import validate_config

            validate_config(config)
        except HotspotError as error:
            self._set_status(str(error), kind="failed")
            return

        def create_and_start() -> tuple[object, NetworkInfo]:
            try:
                self._platform_manager.create_hotspot(config)
                self._platform_manager.start_hotspot()
                status = self._platform_manager.get_status()
                if status.state is not HotspotState.ACTIVE:
                    raise HotspotError(
                        "The operating system did not confirm that the hotspot is active."
                    )
                return status, self._platform_manager.get_network_info()
            except Exception:
                try:
                    self._platform_manager.stop_hotspot()
                except Exception:
                    logger.exception("Hotspot cleanup after creation failure was incomplete.")
                raise

        self._launch_task(
            create_and_start,
            working_text="Creating the hotspot…",
            on_success=self._hotspot_created,
            on_failure=self._hotspot_creation_failed,
        )

    def _hotspot_created(self, result: object) -> None:
        status, network_info = result
        self._active = True
        self._last_network_info = network_info
        self._set_status(status.message or "Hotspot is active.", kind="active")
        self._display_network_info(network_info)
        self._status_timer.start()
        self._update_buttons()

    def _display_network_info(self, network_info: NetworkInfo) -> None:
        self._last_network_info = network_info
        lines = [f"SSID: {self._ssid_input.text()}"]
        if network_info.interface_name:
            lines.append(f"Host interface: {network_info.interface_name}")
        if network_info.ipv4_addresses:
            lines.append(f"Host local IP: {', '.join(network_info.ipv4_addresses)}")
        elif network_info.message:
            lines.append(f"Local IP: {network_info.message}")
        else:
            lines.append("Local IP: Windows did not report an address.")
        self._network_info_label.setText("\n".join(lines))

    def _open_session(self, use_hotspot: bool) -> None:
        """Open the session dashboard over the selected local network."""
        if self._busy:
            return
        preferred_ip = None
        if use_hotspot:
            if not self._active:
                return
            info = self._last_network_info or self._platform_manager.get_network_info()
            preferred_ip = info.ipv4_addresses[0] if info.ipv4_addresses else None
        from sharespace.ui.session_host_dialog import SessionHostDialog

        dialog = SessionHostDialog(
            session_name=self._session_name_input.text(),
            preferred_host_ip=preferred_ip,
            hotspot_manager=self._platform_manager if use_hotspot else None,
            stop_hotspot_on_end=use_hotspot,
            parent=self,
        )
        dialog.exec()

    def _hotspot_creation_failed(self, message: str) -> None:
        self._active = False
        self._status_timer.stop()
        self._set_status(f"Hotspot creation failed: {message}", kind="failed")
        self._network_info_label.setText("No hotspot network information is available.")
        self._update_buttons()

    def _stop_hotspot(self) -> None:
        self._status_timer.stop()

        def stop() -> object:
            self._platform_manager.stop_hotspot()
            return self._platform_manager.get_status()

        self._launch_task(
            stop,
            working_text="Stopping the hotspot…",
            on_success=self._hotspot_stopped,
            on_failure=self._operation_failed,
        )

    def _hotspot_stopped(self, status: object) -> None:
        self._active = status.state is not HotspotState.STOPPED
        if self._active:
            if status.state is HotspotState.ACTIVE:
                message = "The operating system still reports the hotspot as active."
            else:
                message = "ShareSpace could not confirm that the hotspot has stopped."
            self._set_status(message, kind="failed")
            self._status_timer.start()
        else:
            self._set_status("Hotspot stopped.", kind="stopped")
            self._network_info_label.setText(
                "Hotspot stopped. Previous wireless settings are restored only when safe."
            )
        self._update_buttons()
        if self._close_after_stop and not self._active:
            self._close_after_stop = False
            self.accept()

    def _operation_failed(self, message: str) -> None:
        self._set_status(f"Hotspot operation failed: {message}", kind="failed")
        self._update_buttons()

    def _compatibility_failed(self, message: str) -> None:
        self._selected_support = None
        self._set_status(f"Hardware check failed: {message}", kind="failed")
        self._update_buttons()

    def _poll_status(self) -> None:
        if self._busy or not self._active:
            return
        self._launch_task(
            self._platform_manager.get_status,
            working_text="Checking hotspot status…",
            on_success=self._status_polled,
            on_failure=self._operation_failed,
        )

    def _status_polled(self, status: object) -> None:
        if status.state is HotspotState.ACTIVE:
            self._set_status(status.message, kind="active")
            return
        if status.state is HotspotState.STOPPED:
            self._active = False
            self._status_timer.stop()
            self._set_status("Hotspot stopped.", kind="stopped")
        else:
            self._active = True
            self._set_status(f"Hotspot status unknown: {status.message}", kind="failed")
        if status.state is HotspotState.STOPPED:
            self._network_info_label.setText("The hotspot is no longer active.")
        self._update_buttons()

    def closeEvent(self, event) -> None:
        if self._busy:
            event.ignore()
            return
        if self._active:
            answer = QMessageBox.question(
                self,
                "Stop hotspot?",
                "The hotspot is active. Stop it before closing this window?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._close_after_stop = True
                self._stop_hotspot()
            event.ignore()
            return
        super().closeEvent(event)
