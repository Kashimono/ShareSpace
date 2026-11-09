"""NetworkManager/nmcli implementation of the shared hotspot interface."""

from __future__ import annotations

import ipaddress
import logging
import shutil
import subprocess
import uuid
from collections.abc import Callable, Sequence

from sharespace.platform.base_hotspot import (
    HotspotAdapter,
    HotspotConfig,
    HotspotError,
    HotspotState,
    HotspotStatus,
    NetworkInfo,
    SupportCheck,
    WifiAdapter,
    validate_config,
)

logger = logging.getLogger(__name__)

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run_nmcli(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run nmcli without a shell and without logging arguments or secrets."""
    return subprocess.run(
        ["nmcli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=75,
        check=False,
    )


def _split_terse_row(row: str) -> list[str]:
    """Split nmcli's escaped colon-delimited output."""
    fields: list[str] = []
    current: list[str] = []
    escaped = False
    for char in row:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current.clear()
        else:
            current.append(char)
    if escaped:
        current.append("\\")
    fields.append("".join(current))
    return fields


def _parse_nmcli_properties(output: str) -> dict[str, str]:
    properties: dict[str, str] = {}
    for line in output.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            properties[key.strip()] = value.strip()
    return properties


class LinuxHotspotAdapter(HotspotAdapter):
    """Create a temporary WPA hotspot profile through NetworkManager."""

    def __init__(
        self,
        *,
        runner: CommandRunner = _run_nmcli,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._runner = runner
        self._which = which
        self._adapter_id: str | None = None
        self._profile_uuid: str | None = None
        self._previous_connection_uuid: str | None = None
        self._config: HotspotConfig | None = None

    def _run(self, *args: str, secret: str | None = None) -> str:
        try:
            result = self._runner(list(args))
        except (OSError, subprocess.TimeoutExpired) as error:
            message = str(error)
            if secret:
                message = message.replace(secret, "[redacted]")
            logger.warning("NetworkManager command could not complete: %s", message)
            raise HotspotError(f"NetworkManager command could not complete: {message}") from error

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "unknown NetworkManager error").strip()
            if secret:
                message = message.replace(secret, "[redacted]")
            logger.warning("NetworkManager operation failed: %s", message)
            raise HotspotError(message)
        return result.stdout.strip()

    def _ensure_network_manager(self) -> None:
        if self._which("nmcli") is None:
            raise HotspotError("NetworkManager's nmcli is not installed.")
        output = self._run("--terse", "--fields", "RUNNING", "general", "status")
        if output.casefold() not in {"running", "yes", "true"}:
            raise HotspotError("NetworkManager is installed but is not running.")

    def detect_adapters(self) -> list[WifiAdapter]:
        self._ensure_network_manager()
        output = self._run(
            "--terse",
            "--escape",
            "yes",
            "--fields",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        )
        adapters: list[WifiAdapter] = []
        for row in output.splitlines():
            columns = _split_terse_row(row)
            if len(columns) < 4 or columns[1].casefold() not in {"wifi", "802-11-wireless"}:
                continue
            device, _device_type, state, connection = columns[:4]
            active_connection = connection if connection not in {"", "--"} else None
            adapters.append(
                WifiAdapter(
                    identifier=device,
                    name=device,
                    description="NetworkManager wireless interface",
                    state=state,
                    active_connection=active_connection,
                )
            )
        return adapters

    def check_support(self, adapter_id: str) -> SupportCheck:
        try:
            adapters = self.detect_adapters()
        except HotspotError as error:
            return SupportCheck(False, str(error), adapter_id)
        adapter = next((item for item in adapters if item.identifier == adapter_id), None)
        if adapter is None:
            return SupportCheck(False, "The selected wireless adapter is no longer available.", adapter_id)

        try:
            output = self._run(
                "--terse",
                "--fields",
                "WIFI-PROPERTIES.AP",
                "device",
                "show",
                adapter_id,
            )
        except HotspotError as error:
            return SupportCheck(False, str(error), adapter_id)
        values = [line.rsplit(":", 1)[-1].strip().casefold() for line in output.splitlines()]
        ap_value = values[-1] if values else ""
        if ap_value not in {"yes", "no"}:
            return SupportCheck(
                False,
                "NetworkManager could not determine whether this adapter supports access-point mode.",
                adapter_id,
            )
        if ap_value == "no":
            return SupportCheck(
                False,
                "This adapter or its current driver does not report access-point mode support.",
                adapter_id,
            )
        requires_disconnect = adapter.is_connected
        message = "Access-point mode is supported."
        if requires_disconnect:
            message += (
                f" {adapter.name} is currently connected to "
                f"{adapter.active_connection or 'another network'}; creating a hotspot on it "
                "will interrupt that connection."
            )
        return SupportCheck(True, message, adapter_id, requires_disconnect)

    def _device_connection(self, adapter_id: str) -> tuple[str, str | None]:
        output = self._run(
            "--terse",
            "--fields",
            "GENERAL.STATE,GENERAL.CON-UUID",
            "device",
            "show",
            adapter_id,
        )
        properties = _parse_nmcli_properties(output)
        state = properties.get("GENERAL.STATE", "")
        connection_uuid = properties.get("GENERAL.CON-UUID")
        if connection_uuid in {None, "", "--"}:
            connection_uuid = None
        return state, connection_uuid

    def create_hotspot(self, config: HotspotConfig) -> None:
        validate_config(config)
        if self._profile_uuid is not None:
            raise HotspotError("A ShareSpace hotspot is already prepared.")
        support = self.check_support(config.adapter_id)
        if not support.supported:
            raise HotspotError(support.message)
        if support.requires_disconnect and not config.confirm_disconnect:
            raise HotspotError("Confirm the wireless interruption before creating the hotspot.")

        self._ensure_network_manager()
        _state, previous_uuid = self._device_connection(config.adapter_id)
        profile_uuid = str(uuid.uuid4())
        profile_name = f"ShareSpace Hotspot {profile_uuid[:8]}"
        add_args = [
            "--wait",
            "30",
            "connection",
            "add",
            "save",
            "no",
            "connection.id",
            profile_name,
            "connection.type",
            "802-11-wireless",
            "connection.interface-name",
            config.adapter_id,
            "connection.autoconnect",
            "no",
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.ssid",
            config.ssid,
            "802-11-wireless-security.key-mgmt",
            "wpa-psk",
            "802-11-wireless-security.psk",
            config.password,
            "ipv4.method",
            "shared",
            "ipv6.method",
            "ignore",
        ]
        try:
            self._run(*add_args, secret=config.password)
            created_uuid = self._run(
                "--terse",
                "--fields",
                "UUID",
                "connection",
                "show",
                "id",
                profile_name,
            )
            uuid.UUID(created_uuid)
        except Exception as error:
            try:
                self._run("--wait", "20", "connection", "delete", "id", profile_name)
            except Exception:
                logger.warning("Could not remove a partially created ShareSpace profile.")
            if isinstance(error, HotspotError):
                raise
            raise HotspotError(f"Could not identify the new NetworkManager profile: {error}") from error
        self._adapter_id = config.adapter_id
        self._profile_uuid = created_uuid
        self._previous_connection_uuid = previous_uuid
        self._config = config
        logger.info("Prepared a temporary ShareSpace hotspot profile on %s.", config.adapter_id)

    def start_hotspot(self) -> None:
        if not self._profile_uuid or not self._adapter_id or not self._config:
            raise HotspotError("Create a hotspot configuration before starting it.")
        try:
            self._run(
                "--wait",
                "60",
                "connection",
                "up",
                "uuid",
                self._profile_uuid,
                "ifname",
                self._adapter_id,
                secret=self._config.password,
            )
            state, connection_uuid = self._device_connection(self._adapter_id)
            if connection_uuid != self._profile_uuid or not state.casefold().startswith("100"):
                raise HotspotError(
                    "NetworkManager returned without confirming the ShareSpace hotspot is active."
                )
            logger.info("ShareSpace hotspot is active on %s.", self._adapter_id)
        except Exception as error:
            logger.warning("Hotspot start failed; cleaning up its temporary profile.")
            try:
                self._cleanup(restore_previous=True)
            except Exception:
                logger.exception("Cleanup after failed hotspot start was incomplete.")
            if isinstance(error, HotspotError):
                raise
            raise HotspotError(f"Hotspot creation failed: {error}") from error

    def _cleanup(self, *, restore_previous: bool) -> None:
        profile_uuid = self._profile_uuid
        adapter_id = self._adapter_id
        previous_uuid = self._previous_connection_uuid
        if not profile_uuid:
            return

        errors: list[str] = []
        current_uuid: str | None = None
        if adapter_id:
            try:
                _state, current_uuid = self._device_connection(adapter_id)
            except HotspotError as error:
                errors.append(f"Could not inspect the wireless interface before cleanup: {error}")

        if current_uuid == profile_uuid:
            try:
                self._run("--wait", "20", "connection", "down", "uuid", profile_uuid)
            except HotspotError as error:
                errors.append(f"Could not stop the hotspot connection cleanly: {error}")

        try:
            self._run("--wait", "20", "connection", "delete", "uuid", profile_uuid)
        except HotspotError as error:
            errors.append(f"Could not remove the ShareSpace profile: {error}")
        else:
            self._profile_uuid = None
            self._config = None

        if restore_previous and previous_uuid and adapter_id and self._profile_uuid is None:
            try:
                _state, current_uuid = self._device_connection(adapter_id)
                if current_uuid is None:
                    self._run(
                        "--wait",
                        "60",
                        "connection",
                        "up",
                        "uuid",
                        previous_uuid,
                        "ifname",
                        adapter_id,
                    )
                elif current_uuid != profile_uuid:
                    logger.info(
                        "Leaving the current connection on %s unchanged during restoration.",
                        adapter_id,
                    )
            except HotspotError as error:
                errors.append(f"Could not restore the previous wireless connection: {error}")

        if self._profile_uuid is None:
            self._adapter_id = None
            self._previous_connection_uuid = None
        if errors:
            raise HotspotError("; ".join(errors))

    def stop_hotspot(self) -> None:
        """Deactivate/delete the app-owned profile and restore prior Wi-Fi if safe."""
        self._cleanup(restore_previous=True)
        logger.info("ShareSpace hotspot stopped and its temporary profile removed.")

    def get_status(self) -> HotspotStatus:
        if not self._profile_uuid or not self._adapter_id:
            return HotspotStatus(HotspotState.STOPPED, "Hotspot is stopped.")
        try:
            state, connection_uuid = self._device_connection(self._adapter_id)
        except HotspotError as error:
            return HotspotStatus(HotspotState.UNKNOWN, str(error), self._adapter_id)
        if connection_uuid == self._profile_uuid and state.casefold().startswith("100"):
            return HotspotStatus(
                HotspotState.ACTIVE,
                "Hotspot is active.",
                self._adapter_id,
            )
        return HotspotStatus(HotspotState.STOPPED, "Hotspot is stopped.", self._adapter_id)

    def get_network_info(self) -> NetworkInfo:
        if not self._adapter_id or self.get_status().state is not HotspotState.ACTIVE:
            return NetworkInfo(message="The hotspot interface is not active.")
        try:
            output = self._run(
                "--terse",
                "--fields",
                "IP4.ADDRESS",
                "device",
                "show",
                self._adapter_id,
            )
        except HotspotError as error:
            return NetworkInfo(self._adapter_id, message=str(error))

        addresses: list[str] = []
        for line in output.splitlines():
            value = line.rsplit(":", 1)[-1].strip()
            if value in {"", "--"}:
                continue
            try:
                address = ipaddress.ip_interface(value).ip
            except ValueError:
                continue
            if isinstance(address, ipaddress.IPv4Address) and not address.is_loopback:
                addresses.append(str(address))
        if not addresses:
            return NetworkInfo(
                self._adapter_id,
                message="NetworkManager did not report an IPv4 address for the hotspot interface.",
            )
        return NetworkInfo(self._adapter_id, tuple(dict.fromkeys(addresses)))
