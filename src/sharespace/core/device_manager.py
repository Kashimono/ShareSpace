"""Cross-platform local device and interface metadata."""

from __future__ import annotations

import platform
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_address

import psutil


@dataclass(frozen=True)
class LocalInterfaceAddress:
    interface_name: str
    address: str


class DeviceManager:
    """Report local device identity and usable IPv4 interface addresses."""

    def __init__(self, *, hostname: str | None = None, operating_system: str | None = None) -> None:
        self.hostname = hostname or socket.gethostname()
        self.operating_system = operating_system or platform.system()

    def get_local_addresses(self) -> list[LocalInterfaceAddress]:
        """Return assigned, non-loopback IPv4 addresses on interfaces marked up."""
        results: list[LocalInterfaceAddress] = []
        interface_stats = psutil.net_if_stats()
        for interface_name, addresses in psutil.net_if_addrs().items():
            stats = interface_stats.get(interface_name)
            if stats is not None and not stats.isup:
                continue
            for item in addresses:
                if item.family != socket.AF_INET:
                    continue
                address = ip_address(item.address)
                if (
                    address.is_loopback
                    or address.is_unspecified
                    or address.is_global
                ):
                    continue
                results.append(LocalInterfaceAddress(interface_name, str(address)))
        return sorted(results, key=self._address_sort_key)

    def select_host_address(self, preferred: str | None = None) -> LocalInterfaceAddress | None:
        """Prefer a supplied hotspot address, then a private IPv4 address."""
        addresses = self.get_local_addresses()
        if preferred:
            parsed_preferred = ip_address(preferred)
            matching = next((item for item in addresses if item.address == str(parsed_preferred)), None)
            if matching:
                return matching
            if (
                isinstance(parsed_preferred, IPv4Address)
                and not parsed_preferred.is_global
                and not parsed_preferred.is_loopback
                and not parsed_preferred.is_unspecified
            ):
                return LocalInterfaceAddress("preferred", str(parsed_preferred))
        return next((item for item in addresses if ip_address(item.address).is_private), None) or (
            addresses[0] if addresses else None
        )

    @staticmethod
    def _address_sort_key(item: LocalInterfaceAddress) -> tuple[int, str, str]:
        address = ip_address(item.address)
        return (0 if address.is_private else 1, item.interface_name, item.address)
