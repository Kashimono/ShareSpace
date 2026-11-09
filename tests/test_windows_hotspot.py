import json
import subprocess
from enum import Enum
from types import SimpleNamespace

import pytest

from sharespace.platform.base_hotspot import (
    HotspotConfig,
    HotspotError,
    HotspotState,
    HotspotStatus,
    NetworkInfo,
    SupportCheck,
)
from sharespace.platform.windows.hotspot import (
    WindowsHotspotAdapter,
    WindowsTetheringBackend,
)


ADAPTERS_JSON = json.dumps(
    [
        {
            "Name": "Wi-Fi",
            "Description": "Example Wireless Adapter",
            "InterfaceGuid": "{11111111-1111-1111-1111-111111111111}",
            "State": "Up",
            "ActiveConnection": None,
        },
        {
            "Name": "Wi-Fi 2",
            "Description": "USB Wireless Adapter",
            "InterfaceGuid": "{22222222-2222-2222-2222-222222222222}",
            "State": "Up",
            "ActiveConnection": "Home Wi-Fi",
        },
    ]
)


class FakeWindowsBackend:
    def __init__(self, *, supported=True, fail_start=False):
        self.supported = supported
        self.fail_start = fail_start
        self.state = HotspotState.STOPPED
        self.config = None
        self.create_count = 0
        self.start_count = 0
        self.stop_count = 0

    def check_support(self, adapter_id):
        return SupportCheck(
            self.supported,
            "supported" if self.supported else "package capability missing",
            adapter_id,
        )

    def create_hotspot(self, config):
        self.config = config
        self.create_count += 1

    def start_hotspot(self):
        self.start_count += 1
        if self.fail_start:
            raise HotspotError("driver refused hotspot")
        self.state = HotspotState.ACTIVE

    def stop_hotspot(self):
        self.stop_count += 1
        self.state = HotspotState.STOPPED
        self.config = None

    def get_status(self):
        return HotspotStatus(self.state, self.state.value)


class FakePowerShell:
    def __init__(self):
        self.scripts = []

    def __call__(self, args):
        args = list(args)
        script = args[-1]
        self.scripts.append(script)
        if "Get-NetIPAddress" in script:
            stdout = json.dumps(
                [
                    {"InterfaceName": "Local Area Connection* 12", "IPAddress": "192.168.137.1"},
                    {"InterfaceName": "Local Area Connection* 12", "IPAddress": "169.254.1.1"},
                ]
            )
        else:
            stdout = ADAPTERS_JSON
        return subprocess.CompletedProcess(args, 0, stdout, "")


def make_adapter(backend):
    runner = FakePowerShell()
    adapter = WindowsHotspotAdapter(
        backend=backend,
        runner=runner,
        which=lambda _name: "powershell.exe",
    )
    return adapter, runner


def test_windows_adapter_detection_and_support_are_delegated():
    backend = FakeWindowsBackend(supported=False)
    adapter, _runner = make_adapter(backend)

    devices = adapter.detect_adapters()
    check = adapter.check_support(devices[0].identifier)

    assert [device.name for device in devices] == ["Wi-Fi", "Wi-Fi 2"]
    assert check.supported is False
    assert check.message == "package capability missing"


def test_windows_hotspot_start_status_network_info_and_cleanup():
    backend = FakeWindowsBackend()
    adapter, runner = make_adapter(backend)
    adapter.detect_adapters()
    config = HotspotConfig(
        "ShareSpace",
        "safe-password",
        "{11111111-1111-1111-1111-111111111111}",
    )

    adapter.create_hotspot(config)
    adapter.start_hotspot()
    assert adapter.get_status().state is HotspotState.ACTIVE
    assert adapter.get_network_info() == NetworkInfo(
        "Local Area Connection* 12",
        ("192.168.137.1",),
    )
    assert any("Wi-Fi Direct Virtual Adapter" in script for script in runner.scripts)

    adapter.stop_hotspot()

    assert backend.stop_count == 1
    assert adapter.get_status().state is HotspotState.STOPPED


def test_windows_does_not_claim_success_when_backend_start_fails():
    backend = FakeWindowsBackend(fail_start=True)
    adapter, _runner = make_adapter(backend)
    adapter.detect_adapters()
    adapter.create_hotspot(
        HotspotConfig(
            "ShareSpace",
            "safe-password",
            "{11111111-1111-1111-1111-111111111111}",
        )
    )

    with pytest.raises(HotspotError, match="driver refused"):
        adapter.start_hotspot()

    assert backend.stop_count == 1
    assert adapter.get_status().state is HotspotState.STOPPED


def test_windows_permission_query_failure_is_not_reported_as_supported():
    backend = FakeWindowsBackend(supported=False)
    adapter, _runner = make_adapter(backend)
    adapter.detect_adapters()
    support = adapter.check_support("{11111111-1111-1111-1111-111111111111}")

    assert support.supported is False
    with pytest.raises(HotspotError, match="package capability missing"):
        adapter.create_hotspot(
            HotspotConfig(
                "ShareSpace",
                "safe-password",
                "{11111111-1111-1111-1111-111111111111}",
            )
        )
    assert backend.create_count == 0


def test_unpacked_windows_development_install_is_reported_unsupported(monkeypatch):
    monkeypatch.setattr(
        WindowsTetheringBackend,
        "_has_package_identity",
        staticmethod(lambda: False),
    )

    support = WindowsTetheringBackend().check_support("adapter-guid")

    assert support.supported is False
    assert "packaged ShareSpace app" in support.message


def test_windows_status_uses_the_api_enum_name():
    class OperationalState(Enum):
        ON = 1
        OFF = 0

    backend = WindowsTetheringBackend()
    backend._manager = SimpleNamespace(tethering_operational_state=OperationalState.ON)
    assert backend.get_status().state is HotspotState.ACTIVE
    backend._manager.tethering_operational_state = OperationalState.OFF
    assert backend.get_status().state is HotspotState.STOPPED
