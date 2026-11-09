"""Operating-system selection and shared hotspot facade."""

from __future__ import annotations

import platform
import logging
from typing import Callable

from sharespace.platform.base_hotspot import (
    HotspotAdapter,
    HotspotConfig,
    HotspotError,
    HotspotState,
    HotspotStatus,
    NetworkInfo,
    SupportCheck,
    WifiAdapter,
)

logger = logging.getLogger(__name__)


class UnsupportedPlatformAdapter(HotspotAdapter):
    """Safe fallback for operating systems with no hotspot implementation."""

    def detect_adapters(self) -> list[WifiAdapter]:
        return []

    def check_support(self, adapter_id: str) -> SupportCheck:
        return SupportCheck(False, "Hotspot control is not supported on this operating system.")

    def create_hotspot(self, config: HotspotConfig) -> None:
        raise HotspotError("Hotspot control is not supported on this operating system.")

    def start_hotspot(self) -> None:
        raise HotspotError("Hotspot control is not supported on this operating system.")

    def stop_hotspot(self) -> None:
        return None

    def get_status(self) -> HotspotStatus:
        return HotspotStatus(HotspotState.UNKNOWN, "Hotspot control is unsupported.")

    def get_network_info(self) -> NetworkInfo:
        return NetworkInfo(message="Hotspot control is unsupported.")


class PlatformManager:
    """Shared facade over one OS-specific hotspot adapter."""

    def __init__(self, adapter: HotspotAdapter) -> None:
        self.adapter = adapter

    def detect_adapters(self) -> list[WifiAdapter]:
        return self.adapter.detect_adapters()

    def check_support(self, adapter_id: str) -> SupportCheck:
        return self.adapter.check_support(adapter_id)

    def create_hotspot(self, config: HotspotConfig) -> None:
        self.adapter.create_hotspot(config)

    def start_hotspot(self) -> None:
        self.adapter.start_hotspot()

    def stop_hotspot(self) -> None:
        self.adapter.stop_hotspot()

    def get_status(self) -> HotspotStatus:
        return self.adapter.get_status()

    def get_network_info(self) -> NetworkInfo:
        return self.adapter.get_network_info()

    def cleanup_on_exit(self) -> None:
        """Ask the selected adapter to remove only resources it owns."""
        try:
            self.adapter.stop_hotspot()
        except Exception:
            logger.exception("Could not complete hotspot cleanup during application shutdown.")


def create_platform_manager(
    system_name: str | None = None,
    *,
    windows_factory: Callable[[], HotspotAdapter] | None = None,
    linux_factory: Callable[[], HotspotAdapter] | None = None,
) -> PlatformManager:
    """Select the adapter matching the host operating system."""
    system_name = (system_name or platform.system()).casefold()
    if system_name == "windows":
        if windows_factory is None:
            from sharespace.platform.windows.hotspot import WindowsHotspotAdapter

            windows_factory = WindowsHotspotAdapter
        return PlatformManager(windows_factory())
    if system_name == "linux":
        if linux_factory is None:
            from sharespace.platform.linux.hotspot import LinuxHotspotAdapter

            linux_factory = LinuxHotspotAdapter
        return PlatformManager(linux_factory())
    return PlatformManager(UnsupportedPlatformAdapter())
