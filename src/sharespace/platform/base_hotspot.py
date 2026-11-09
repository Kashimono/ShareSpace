"""OS-neutral hotspot models and adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class HotspotError(RuntimeError):
    """A hotspot operation failed or could not be completed safely."""


class HotspotState(str, Enum):
    STOPPED = "stopped"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WifiAdapter:
    """A wireless adapter available to ShareSpace."""

    identifier: str
    name: str
    description: str = ""
    state: str = "unknown"
    active_connection: str | None = None
    supports_ap: bool | None = None

    @property
    def is_connected(self) -> bool:
        """Whether this radio currently carries a non-hotspot connection."""
        return bool(self.active_connection) or self.state.casefold() in {
            "connected",
            "100 (connected)",
        }


@dataclass(frozen=True)
class HotspotConfig:
    """SSID, WPA passphrase and selected local wireless adapter."""

    ssid: str
    password: str
    adapter_id: str
    confirm_disconnect: bool = False


@dataclass(frozen=True)
class SupportCheck:
    """Hardware, OS and permission check result."""

    supported: bool
    message: str
    adapter_id: str | None = None
    requires_disconnect: bool = False
    requires_confirmation: bool = False
    confirmation_message: str | None = None


@dataclass(frozen=True)
class HotspotStatus:
    """Observed hotspot state. State is never inferred from a requested action."""

    state: HotspotState
    message: str
    adapter_id: str | None = None


@dataclass(frozen=True)
class NetworkInfo:
    """Host-side interface information for the active hotspot."""

    interface_name: str | None = None
    ipv4_addresses: tuple[str, ...] = ()
    message: str = ""


def validate_config(config: HotspotConfig) -> None:
    """Validate the common Wi-Fi SSID and WPA2 passphrase constraints."""
    if not config.adapter_id:
        raise HotspotError("Select a wireless adapter before creating a hotspot.")
    if not 1 <= len(config.ssid) <= 32:
        raise HotspotError("SSID must contain between 1 and 32 characters.")
    if config.ssid != config.ssid.strip():
        raise HotspotError("SSID cannot start or end with a space.")
    if not config.ssid.isascii() or any(not 0x20 <= ord(char) <= 0x7E for char in config.ssid):
        raise HotspotError("SSID must use printable ASCII characters for Windows compatibility.")
    if not 8 <= len(config.password) <= 63:
        raise HotspotError("Wi-Fi password must contain between 8 and 63 characters.")
    if config.password != config.password.strip():
        raise HotspotError("Wi-Fi password cannot start or end with a space.")
    if not config.password.isascii() or any(
        not 0x20 <= ord(char) <= 0x7E for char in config.password
    ):
        raise HotspotError("Wi-Fi password must use printable ASCII characters.")


class HotspotAdapter(ABC):
    """Contract implemented by each supported operating system."""

    @abstractmethod
    def detect_adapters(self) -> list[WifiAdapter]:
        """Return wireless adapters visible to the OS networking service."""

    @abstractmethod
    def check_support(self, adapter_id: str) -> SupportCheck:
        """Check OS, permission and selected-radio AP support."""

    @abstractmethod
    def create_hotspot(self, config: HotspotConfig) -> None:
        """Prepare an app-owned hotspot resource without claiming it is active."""

    @abstractmethod
    def start_hotspot(self) -> None:
        """Start the prepared hotspot and raise unless the OS confirms success."""

    @abstractmethod
    def stop_hotspot(self) -> None:
        """Stop and remove only resources created by this adapter instance."""

    @abstractmethod
    def get_status(self) -> HotspotStatus:
        """Query the OS for current hotspot state."""

    @abstractmethod
    def get_network_info(self) -> NetworkInfo:
        """Return the hotspot interface and its non-loopback IPv4 addresses."""
