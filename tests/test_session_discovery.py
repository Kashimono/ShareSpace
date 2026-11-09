from sharespace.core.device_manager import DeviceManager, LocalInterfaceAddress
from sharespace.core.session_manager import SessionManager
from sharespace.networking.discovery import SERVICE_TYPE, SessionAdvertiser, SessionDiscovery


class FakeServiceInfo:
    def __init__(self, session_id, session_name, host_name, host_os, port=54832, count=1):
        self.port = port
        self.properties = {
            b"session_id": session_id.encode(),
            b"session_name": session_name.encode(),
            b"host_name": host_name.encode(),
            b"host_os": host_os.encode(),
            b"participant_count": str(count).encode(),
            b"status": b"active",
            b"created_at": b"2026-01-01T00:00:00+00:00",
            b"protocol": b"1",
        }

    def parsed_addresses(self):
        return ["192.168.1.20"]


class FakeZeroconf:
    def __init__(self, records):
        self.records = records

    def get_service_info(self, _type, name, timeout):
        return self.records.get(name)


def make_manager():
    manager = SessionManager()
    session, credentials = manager.create_session(
        session_name="mDNS room",
        host_device_name="Host PC",
        host_os="Windows",
        host_ip="192.168.1.20",
        port=54832,
    )
    return manager, session, credentials


def test_discovery_deduplicates_by_session_id_and_removes_service_records():
    session_id = "1" * 32
    name_a = f"{session_id}.{SERVICE_TYPE}"
    name_b = f"{session_id}-duplicate.{SERVICE_TYPE}"
    records = {
        name_a: FakeServiceInfo(session_id, "Room", "Host", "Windows"),
        name_b: FakeServiceInfo(session_id, "Room", "Host", "Windows", count=2),
    }
    discovery = SessionDiscovery(stale_after_seconds=30)
    fake = FakeZeroconf(records)

    discovery._resolve(fake, SERVICE_TYPE, name_a)
    discovery._resolve(fake, SERVICE_TYPE, name_b)
    sessions = discovery.sessions()

    assert len(sessions) == 1
    assert sessions[0].participant_count == 2
    discovery._remove(name_b)
    assert discovery.sessions() == []


def test_stale_advertisement_is_removed_from_session_list():
    discovery = SessionDiscovery(stale_after_seconds=2)
    session_id = "2" * 32
    name = f"{session_id}.{SERVICE_TYPE}"
    discovery._resolve(
        FakeZeroconf({name: FakeServiceInfo(session_id, "Old room", "Host", "Linux")}),
        SERVICE_TYPE,
        name,
    )
    seen_at = discovery._by_session_id[session_id].last_seen_monotonic

    assert discovery.sessions(now_monotonic=seen_at + 3) == []


def test_advertisements_contain_public_metadata_but_no_join_or_host_credentials():
    manager, session, credentials = make_manager()
    properties = SessionAdvertiser._properties(session)
    text = b" ".join(properties.values()).decode("utf-8")

    assert "session_id" in properties
    assert "session_name" in properties
    assert "participant_count" in properties
    assert credentials.joining_code not in text
    assert credentials.host_token not in text


def test_device_manager_prefers_the_hotspot_address_without_hardcoding_addresses():
    manager = DeviceManager(hostname="device", operating_system="Linux")
    manager.get_local_addresses = lambda: [
        LocalInterfaceAddress("wlan0", "192.168.4.1"),
        LocalInterfaceAddress("eth0", "192.168.1.20"),
    ]

    selected = manager.select_host_address("192.168.4.1")

    assert selected == LocalInterfaceAddress("wlan0", "192.168.4.1")
