from sharespace.platform.base_hotspot import HotspotState, HotspotStatus, SupportCheck
from sharespace.platform.platform_manager import (
    PlatformManager,
    UnsupportedPlatformAdapter,
    create_platform_manager,
)


class StubAdapter(UnsupportedPlatformAdapter):
    def check_support(self, adapter_id):
        return SupportCheck(True, "supported", adapter_id)

    def get_status(self):
        return HotspotStatus(HotspotState.STOPPED, "stopped")


def test_platform_manager_selects_windows_and_linux_factories():
    windows = StubAdapter()
    linux = StubAdapter()

    windows_manager = create_platform_manager(
        "Windows",
        windows_factory=lambda: windows,
        linux_factory=lambda: linux,
    )
    linux_manager = create_platform_manager(
        "Linux",
        windows_factory=lambda: windows,
        linux_factory=lambda: linux,
    )

    assert isinstance(windows_manager, PlatformManager)
    assert windows_manager.adapter is windows
    assert linux_manager.adapter is linux
    assert windows_manager.check_support("adapter-1").supported


def test_unknown_platform_uses_safe_unsupported_adapter():
    manager = create_platform_manager("Plan9")

    assert isinstance(manager.adapter, UnsupportedPlatformAdapter)
    assert manager.detect_adapters() == []
    assert manager.check_support("wifi0").supported is False
