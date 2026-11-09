"""Windows hotspot support through the Windows Runtime tethering API.

No hosted-network netsh commands are used. The WinRT API requires a package
identity with the wiFiControl device capability and an active connection profile;
the adapter reports those constraints instead of claiming unsupported operation.
"""

from __future__ import annotations

import asyncio
import ctypes
import ipaddress
import json
import logging
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

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
_ADAPTER_QUERY = r"""
$ErrorActionPreference = 'Stop'
Get-NetAdapter -Physical |
  Where-Object { $_.NdisPhysicalMedium -eq 9 } |
  ForEach-Object {
    $profile = Get-NetConnectionProfile -InterfaceIndex $_.ifIndex -ErrorAction SilentlyContinue |
      Select-Object -First 1
    [pscustomobject]@{
      Name = $_.Name
      Description = $_.InterfaceDescription
      InterfaceGuid = [string]$_.InterfaceGuid
      State = [string]$_.Status
      ActiveConnection = if ($profile) { [string]$profile.Name } else { $null }
    }
  } | ConvertTo-Json -Compress
"""
_HOTSPOT_IP_QUERY = r"""
$ErrorActionPreference = 'Stop'
Get-NetAdapter -IncludeHidden |
  Where-Object {
    $_.Status -eq 'Up' -and $_.InterfaceDescription -match 'Wi-Fi Direct Virtual Adapter'
  } |
  ForEach-Object {
    $name = $_.Name
    Get-NetIPAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
      Where-Object { $_.IPAddress -notlike '169.254.*' } |
      ForEach-Object {
        [pscustomobject]@{ InterfaceName = $name; IPAddress = $_.IPAddress }
      }
  } | ConvertTo-Json -Compress
"""


def _run_powershell(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )


def _json_rows(output: str) -> list[dict[str, Any]]:
    if not output.strip():
        return []
    parsed = json.loads(output)
    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return [row for row in parsed if isinstance(row, dict)]
    return []


class WindowsTetheringBackend:
    """Thin WinRT boundary, isolated so the API can be mocked in tests."""

    def __init__(self) -> None:
        self._manager: Any | None = None
        self._profile: Any | None = None
        self._previous_configuration: Any | None = None
        self._configuration: Any | None = None
        self._per_session_supported = False
        self._configured = False
        self._active = False

    @staticmethod
    def _has_package_identity() -> bool:
        """Check whether Windows launched this process from an MSIX/AppX package."""
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel32.GetCurrentPackageFullName
            function.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
            function.restype = ctypes.c_long
            length = ctypes.c_uint32(0)
            result = function(ctypes.byref(length), None)
            # ERROR_INSUFFICIENT_BUFFER means a package identity exists.
            return result == 122
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _load_winrt() -> tuple[Any, Any, Any, Any]:
        try:
            from winrt.windows.devices.wifi import WiFiAdapter
            from winrt.windows.foundation.metadata import ApiInformation
            from winrt.windows.networking.connectivity import NetworkInformation
            from winrt.windows.networking.networkoperators import (
                NetworkOperatorTetheringAccessPointConfiguration,
                NetworkOperatorTetheringManager,
                NetworkOperatorTetheringSessionAccessPointConfiguration,
                TetheringCapability,
            )
        except (ImportError, OSError) as error:
            raise HotspotError(
                "Windows tethering support requires the ShareSpace Windows Runtime packages."
            ) from error
        api = {
            "wifi_adapter": WiFiAdapter,
            "access_point_configuration": NetworkOperatorTetheringAccessPointConfiguration,
            "session_configuration": NetworkOperatorTetheringSessionAccessPointConfiguration,
            "tethering_capability": TetheringCapability,
            "per_session_supported": ApiInformation.is_api_contract_present(
                "Windows.Foundation.UniversalApiContract",
                19,
            ),
        }
        return NetworkInformation, NetworkOperatorTetheringManager, api

    def _api(self) -> tuple[Any, Any, Any]:
        network_information, manager_type, api = self._load_winrt()
        return network_information, manager_type, api

    async def _find_wifi_adapter(self, adapter_id: str) -> Any | None:
        _network_information, _manager_type, api = self._api()
        wifi_adapters = await api["wifi_adapter"].find_all_adapters_async()
        expected = adapter_id.strip("{}").casefold()
        for wifi_adapter in wifi_adapters:
            candidate = str(wifi_adapter.network_adapter.network_adapter_id).strip("{}").casefold()
            if candidate == expected:
                return wifi_adapter
        return None

    def check_support(self, adapter_id: str) -> SupportCheck:
        if not self._has_package_identity():
            return SupportCheck(
                False,
                "Windows hotspot control requires a packaged ShareSpace app declaring "
                "the wiFiControl capability. The current Python development install is unpackaged.",
                adapter_id,
            )
        try:
            network_information, manager_type, api = self._api()
            profile = network_information.get_internet_connection_profile()
            if profile is None:
                return SupportCheck(
                    False,
                    "Windows tethering requires an active connection profile. Offline operation "
                    "without a connection profile is not available through this API.",
                    adapter_id,
                )
            capability = manager_type.get_tethering_capability_from_connection_profile(profile)
            if capability != api["tethering_capability"].ENABLED:
                reason = getattr(capability, "name", str(capability)).casefold()
                return SupportCheck(
                    False,
                    f"Windows reports tethering unavailable ({reason}). Check hardware, driver, "
                    "system policy, and the wiFiControl package capability.",
                    adapter_id,
                )
            wifi_adapter = asyncio.run(self._find_wifi_adapter(adapter_id))
            if wifi_adapter is None:
                return SupportCheck(
                    False,
                    "The selected Windows wireless adapter is unavailable to the tethering API.",
                    adapter_id,
                )
            return SupportCheck(
                True,
                "Windows reports tethering support. This API shares a connection profile; "
                "offline-only operation still requires hardware verification.",
                adapter_id,
                requires_confirmation=True,
                confirmation_message=(
                    "Windows tethering uses the active connection profile and may share or "
                    "adjust that connection. Offline-only operation is not guaranteed. Continue?"
                ),
            )
        except Exception as error:
            logger.warning("Windows tethering capability check failed: %s", error)
            return SupportCheck(
                False,
                f"Windows could not confirm hotspot support: {error}",
                adapter_id,
            )

    def create_hotspot(self, config: HotspotConfig) -> None:
        validate_config(config)
        support = self.check_support(config.adapter_id)
        if not support.supported:
            raise HotspotError(support.message)
        try:
            network_information, manager_type, api = self._api()
            profile = network_information.get_internet_connection_profile()
            if profile is None:
                raise HotspotError(
                    "Windows no longer exposes the connection profile needed for tethering."
                )
            wifi_adapter = asyncio.run(self._find_wifi_adapter(config.adapter_id))
            if wifi_adapter is None:
                raise HotspotError("The selected wireless adapter is no longer available.")
            manager = manager_type.create_from_connection_profile(
                profile,
                wifi_adapter.network_adapter,
            )
            self._manager = manager
            self._profile = profile
            self._configuration = (config, api)
            self._per_session_supported = api["per_session_supported"]
            self._configured = False
            self._active = False
        except HotspotError:
            raise
        except Exception as error:
            raise HotspotError(f"Windows could not prepare the hotspot: {error}") from error

    async def _start_async(self, config: HotspotConfig, api: dict[str, Any]) -> None:
        manager = self._manager
        if manager is None:
            raise HotspotError("Create a hotspot configuration before starting it.")

        try:
            if self._per_session_supported:
                session_config = api["session_configuration"]()
                session_config.ssid = config.ssid
                session_config.passphrase = config.password
                result = await manager.start_tethering_async(session_config)
            else:
                previous = manager.get_current_access_point_configuration()
                persistent_config = api["access_point_configuration"]()
                persistent_config.ssid = config.ssid
                persistent_config.passphrase = config.password
                await manager.configure_access_point_async(persistent_config)
                self._previous_configuration = previous
                self._configured = True
                result = await manager.start_tethering_async()

            self._require_success(result, "start")
            self._active = True
        except Exception:
            await self._restore_configuration_async(api)
            raise

    @staticmethod
    def _require_success(result: Any, operation: str) -> None:
        if result is None:
            raise HotspotError(f"Windows returned no result for hotspot {operation}.")
        status = getattr(result.status, "name", str(result.status)).casefold()
        if status != "success":
            detail = getattr(result, "additional_error_message", "") or status
            raise HotspotError(f"Windows hotspot {operation} failed: {detail}")

    async def _restore_configuration_async(self, api: dict[str, Any] | None = None) -> None:
        if not self._configured or self._manager is None or self._previous_configuration is None:
            return
        if api is None:
            _network_information, _manager_type, api = self._api()
        await self._manager.configure_access_point_async(self._previous_configuration)
        self._previous_configuration = None
        self._configured = False

    def start_hotspot(self) -> None:
        if self._configuration is None:
            raise HotspotError("Create a hotspot configuration before starting it.")
        config, api = self._configuration
        try:
            asyncio.run(self._start_async(config, api))
        except HotspotError:
            raise
        except Exception as error:
            logger.exception("Windows tethering start failed.")
            raise HotspotError(f"Windows hotspot creation failed: {error}") from error
        if self.get_status().state is not HotspotState.ACTIVE:
            self._active = False
            try:
                asyncio.run(self._restore_configuration_async(api))
            except Exception:
                logger.exception("Could not restore Windows hotspot configuration after failed start.")
            raise HotspotError(
                "Windows did not confirm the hotspot is active after the start operation."
            )

    async def _stop_async(self, api: dict[str, Any]) -> None:
        if self._manager is None:
            self._active = False
            return
        if self._active:
            result = await self._manager.stop_tethering_async()
            self._require_success(result, "stop")
        await self._restore_configuration_async(api)
        self._active = False
        self._configuration = None
        self._manager = None
        self._profile = None

    def stop_hotspot(self) -> None:
        if self._manager is None:
            return
        try:
            _network_information, _manager_type, api = self._api()
            asyncio.run(self._stop_async(api))
        except HotspotError:
            raise
        except Exception as error:
            logger.exception("Windows tethering cleanup failed.")
            raise HotspotError(f"Windows could not stop the hotspot cleanly: {error}") from error

    def get_status(self) -> HotspotStatus:
        if self._manager is None:
            return HotspotStatus(HotspotState.STOPPED, "Hotspot is stopped.")
        state_value = getattr(self._manager, "tethering_operational_state", "")
        state = getattr(state_value, "name", str(state_value)).casefold()
        if state in {"on", "active"}:
            return HotspotStatus(
                HotspotState.ACTIVE,
                "Windows reports the tethering hotspot is active.",
            )
        if state in {"off", "disabled"}:
            return HotspotStatus(HotspotState.STOPPED, "Hotspot is stopped.")
        return HotspotStatus(
            HotspotState.UNKNOWN,
            f"Windows reported an unrecognized tethering state: {state or 'unknown'}.",
        )


class WindowsHotspotAdapter(HotspotAdapter):
    """Windows adapter discovery and Windows Runtime tethering control."""

    def __init__(
        self,
        *,
        backend: WindowsTetheringBackend | None = None,
        runner: CommandRunner = _run_powershell,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._backend = backend or WindowsTetheringBackend()
        self._runner = runner
        self._which = which
        self._adapters: dict[str, WifiAdapter] = {}
        self._config: HotspotConfig | None = None

    def _run_powershell(self, script: str) -> str:
        executable = self._which("powershell.exe") or self._which("pwsh")
        if executable is None:
            raise HotspotError("PowerShell is required to inspect Windows wireless adapters.")
        try:
            result = self._runner(
                [executable, "-NoProfile", "-NonInteractive", "-Command", script]
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise HotspotError(f"Windows adapter query failed: {error}") from error
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "PowerShell query failed").strip()
            raise HotspotError(message)
        return result.stdout.strip()

    def detect_adapters(self) -> list[WifiAdapter]:
        try:
            rows = _json_rows(self._run_powershell(_ADAPTER_QUERY))
        except (HotspotError, json.JSONDecodeError) as error:
            logger.warning("Windows wireless adapter detection failed: %s", error)
            raise HotspotError(str(error)) from error
        adapters: list[WifiAdapter] = []
        for row in rows:
            interface_guid = str(row.get("InterfaceGuid") or "").strip()
            name = str(row.get("Name", "")).strip()
            if not interface_guid or not name:
                continue
            adapter = WifiAdapter(
                identifier=interface_guid,
                name=name,
                description=str(row.get("Description") or ""),
                state=str(row.get("State") or "unknown"),
                active_connection=str(row.get("ActiveConnection") or "") or None,
            )
            adapters.append(adapter)
        self._adapters = {adapter.identifier: adapter for adapter in adapters}
        return adapters

    def check_support(self, adapter_id: str) -> SupportCheck:
        if not self._adapters:
            try:
                self.detect_adapters()
            except HotspotError as error:
                return SupportCheck(False, str(error), adapter_id)
        if adapter_id not in self._adapters:
            return SupportCheck(False, "The selected wireless adapter is unavailable.", adapter_id)
        return self._backend.check_support(adapter_id)

    def create_hotspot(self, config: HotspotConfig) -> None:
        validate_config(config)
        if self._config is not None:
            raise HotspotError("A ShareSpace hotspot is already prepared.")
        support = self.check_support(config.adapter_id)
        if not support.supported:
            raise HotspotError(support.message)
        self._backend.create_hotspot(config)
        self._config = config

    def start_hotspot(self) -> None:
        if self._config is None:
            raise HotspotError("Create a hotspot configuration before starting it.")
        try:
            self._backend.start_hotspot()
            if self._backend.get_status().state is not HotspotState.ACTIVE:
                raise HotspotError(
                    "Windows did not confirm the hotspot is active after the start operation."
                )
        except Exception as error:
            logger.warning("Windows hotspot start failed; attempting adapter cleanup.")
            try:
                self._backend.stop_hotspot()
            except Exception:
                logger.exception("Windows hotspot cleanup after failed start was incomplete.")
            self._config = None
            if isinstance(error, HotspotError):
                raise
            raise HotspotError(f"Windows hotspot creation failed: {error}") from error

    def stop_hotspot(self) -> None:
        self._backend.stop_hotspot()
        self._config = None

    def get_status(self) -> HotspotStatus:
        try:
            return self._backend.get_status()
        except Exception as error:
            logger.warning("Windows hotspot status query failed: %s", error)
            return HotspotStatus(HotspotState.UNKNOWN, f"Windows status query failed: {error}")

    def get_network_info(self) -> NetworkInfo:
        status = self.get_status()
        if status.state is not HotspotState.ACTIVE:
            return NetworkInfo(message="The Windows hotspot interface is not active.")
        try:
            rows = _json_rows(self._run_powershell(_HOTSPOT_IP_QUERY))
        except (HotspotError, json.JSONDecodeError) as error:
            logger.warning("Windows hotspot IP query failed: %s", error)
            return NetworkInfo(message=str(error))
        addresses: list[str] = []
        names: list[str] = []
        for row in rows:
            address = str(row.get("IPAddress") or "")
            try:
                parsed = ipaddress.IPv4Address(address)
            except ValueError:
                continue
            if parsed.is_loopback or parsed.is_link_local:
                continue
            addresses.append(str(parsed))
            name = str(row.get("InterfaceName") or "")
            if name:
                names.append(name)
        if not addresses:
            return NetworkInfo(
                message="Windows reports the hotspot active but did not expose its local IPv4 address.",
            )
        return NetworkInfo(names[0] if names else None, tuple(dict.fromkeys(addresses)))
