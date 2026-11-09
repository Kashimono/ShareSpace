import subprocess

import pytest

from sharespace.platform.base_hotspot import HotspotConfig, HotspotError, HotspotState
from sharespace.platform.linux.hotspot import LinuxHotspotAdapter, _split_terse_row


class FakeNmcli:
    def __init__(
        self,
        *,
        active_uuid="previous-uuid",
        active_name="Home Wi-Fi",
        ap_support="yes",
    ):
        self.active_uuid = active_uuid
        self.active_name = active_name
        self.ap_support = ap_support
        self.profile_uuid = None
        self.profile_name = None
        self.deleted_uuids = []
        self.down_uuids = []
        self.fail_activation = False
        self.fail_delete = False
        self.calls = []

    @staticmethod
    def _completed(args, stdout="", returncode=0, stderr=""):
        return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)

    def __call__(self, args):
        args = list(args)
        self.calls.append(args)
        if args[-2:] == ["general", "status"]:
            return self._completed(args, "running\n")

        if args[-2:] == ["device", "status"]:
            if self.active_uuid == "previous-uuid":
                state, name = "connected", self.active_name
            elif self.active_uuid:
                state, name = "connected", "ShareSpace Hotspot"
            else:
                state, name = "disconnected", "--"
            return self._completed(args, f"wlan0:wifi:{state}:{name}\n")

        if "device" in args and "show" in args:
            fields = args[args.index("--fields") + 1]
            if fields == "WIFI-PROPERTIES.AP":
                return self._completed(args, f"WIFI-PROPERTIES.AP: {self.ap_support}\n")
            if fields == "GENERAL.STATE,GENERAL.CON-UUID":
                uuid_value = self.active_uuid or "--"
                state = "100 (connected)" if self.active_uuid else "30 (disconnected)"
                return self._completed(
                    args,
                    f"GENERAL.STATE: {state}\nGENERAL.CON-UUID: {uuid_value}\n",
                )
            if fields == "IP4.ADDRESS":
                return self._completed(args, "IP4.ADDRESS[1]: 10.42.0.1/24\n")

        if "connection" in args and "add" in args:
            name_index = args.index("connection.id") + 1
            self.profile_name = args[name_index]
            self.profile_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            return self._completed(args, "Connection added\n")

        if "connection" in args and "show" in args and "id" in args:
            return self._completed(args, f"{self.profile_uuid}\n")

        if "connection" in args and "up" in args:
            uuid_value = args[args.index("uuid") + 1]
            if self.fail_activation and uuid_value == self.profile_uuid:
                self.active_uuid = None
                return self._completed(args, returncode=10, stderr="radio rejected AP mode")
            self.active_uuid = uuid_value
            return self._completed(args, "Connection activated\n")

        if "connection" in args and "down" in args:
            uuid_value = args[args.index("uuid") + 1]
            self.down_uuids.append(uuid_value)
            if self.active_uuid == uuid_value:
                self.active_uuid = None
            return self._completed(args, "Connection deactivated\n")

        if "connection" in args and "delete" in args:
            uuid_value = args[args.index("uuid") + 1]
            if self.fail_delete:
                return self._completed(args, returncode=10, stderr="delete denied")
            self.deleted_uuids.append(uuid_value)
            if self.profile_uuid == uuid_value:
                self.profile_uuid = None
            if self.active_uuid == uuid_value:
                self.active_uuid = None
            return self._completed(args, "Connection deleted\n")

        return self._completed(args)


def make_adapter(fake):
    return LinuxHotspotAdapter(runner=fake, which=lambda _name: "/usr/bin/nmcli")


def test_nmcli_escaped_fields_are_parsed():
    assert _split_terse_row(r"wlan0:wifi:connected:Home\:Private") == [
        "wlan0",
        "wifi",
        "connected",
        "Home:Private",
    ]


def test_linux_detects_wireless_adapter_and_access_point_support():
    fake = FakeNmcli(active_uuid=None)
    adapter = make_adapter(fake)

    detected = adapter.detect_adapters()
    support = adapter.check_support("wlan0")

    assert [item.identifier for item in detected] == ["wlan0"]
    assert support.supported is True
    assert support.requires_disconnect is False


def test_linux_rejects_adapter_without_access_point_mode():
    fake = FakeNmcli(active_uuid=None, ap_support="no")
    adapter = make_adapter(fake)

    support = adapter.check_support("wlan0")
    assert support.supported is False
    assert "does not report access-point mode" in support.message
    with pytest.raises(HotspotError, match="does not report"):
        adapter.create_hotspot(HotspotConfig("ShareSpace", "safe-password", "wlan0"))
    assert fake.profile_uuid is None


def test_linux_requires_confirmation_then_starts_and_restores_profile():
    fake = FakeNmcli()
    adapter = make_adapter(fake)
    config = HotspotConfig("ShareSpace", "safe-password", "wlan0")

    support = adapter.check_support("wlan0")
    assert support.supported is True
    assert support.requires_disconnect is True
    with pytest.raises(HotspotError, match="Confirm"):
        adapter.create_hotspot(config)
    assert fake.profile_uuid is None

    adapter.create_hotspot(
        HotspotConfig("ShareSpace", "safe-password", "wlan0", confirm_disconnect=True)
    )
    owned_uuid = fake.profile_uuid
    adapter.start_hotspot()

    assert adapter.get_status().state is HotspotState.ACTIVE
    info = adapter.get_network_info()
    assert info.interface_name == "wlan0"
    assert info.ipv4_addresses == ("10.42.0.1",)
    add_call = next(call for call in fake.calls if "add" in call)
    assert "save" in add_call and add_call[add_call.index("save") + 1] == "no"
    assert "ipv4.method" in add_call and add_call[add_call.index("ipv4.method") + 1] == "shared"

    adapter.stop_hotspot()

    assert owned_uuid in fake.deleted_uuids
    assert fake.active_uuid == "previous-uuid"
    assert adapter.get_status().state is HotspotState.STOPPED


def test_failed_linux_start_removes_profile_and_restores_previous_connection():
    fake = FakeNmcli()
    fake.fail_activation = True
    adapter = make_adapter(fake)
    adapter.create_hotspot(
        HotspotConfig("ShareSpace", "safe-password", "wlan0", confirm_disconnect=True)
    )
    owned_uuid = fake.profile_uuid

    with pytest.raises(HotspotError, match="radio rejected"):
        adapter.start_hotspot()

    assert owned_uuid in fake.deleted_uuids
    assert fake.active_uuid == "previous-uuid"
    assert adapter.get_status().state is HotspotState.STOPPED


def test_stop_does_not_disconnect_a_different_active_profile():
    fake = FakeNmcli(active_uuid=None)
    adapter = make_adapter(fake)
    adapter.create_hotspot(HotspotConfig("ShareSpace", "safe-password", "wlan0"))
    owned_uuid = fake.profile_uuid
    adapter.start_hotspot()
    fake.active_uuid = "user-selected-other-uuid"

    adapter.stop_hotspot()

    assert owned_uuid in fake.deleted_uuids
    assert fake.down_uuids == []
    assert fake.active_uuid == "user-selected-other-uuid"


def test_nmcli_unavailable_is_reported():
    adapter = LinuxHotspotAdapter(runner=FakeNmcli(), which=lambda _name: None)

    with pytest.raises(HotspotError, match="not installed"):
        adapter.detect_adapters()


def test_failed_profile_cleanup_is_reported_and_kept_for_retry():
    fake = FakeNmcli(active_uuid=None)
    adapter = make_adapter(fake)
    adapter.create_hotspot(HotspotConfig("ShareSpace", "safe-password", "wlan0"))
    fake.fail_delete = True

    with pytest.raises(HotspotError, match="delete denied"):
        adapter.stop_hotspot()
    assert adapter.get_status().state is HotspotState.STOPPED
