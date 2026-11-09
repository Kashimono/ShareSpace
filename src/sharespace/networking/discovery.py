"""Zeroconf/mDNS advertisement and discovery for nearby active sessions."""

from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from sharespace.core.device_manager import DeviceManager
from sharespace.core.models import PublicSession, SessionStatus
from sharespace.core.session_manager import SessionManager

logger = logging.getLogger(__name__)
SERVICE_TYPE = "_sharespace._tcp.local."
DISCOVERY_STALE_SECONDS = 180
ADVERTISEMENT_INTERVAL_SECONDS = 15


def _txt_value(value: str, max_bytes: int = 220) -> bytes:
    encoded = value.encode("utf-8")
    return encoded[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


class SessionAdvertiser:
    """Publish public session metadata periodically and withdraw it on stop."""

    def __init__(
        self,
        manager: SessionManager,
        device_manager: DeviceManager | None = None,
        *,
        interval_seconds: float = ADVERTISEMENT_INTERVAL_SECONDS,
    ) -> None:
        self.manager = manager
        self.device_manager = device_manager or DeviceManager()
        self.interval_seconds = interval_seconds
        self._zeroconf: Zeroconf | None = None
        self._info: ServiceInfo | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None

    @property
    def active(self) -> bool:
        return self._zeroconf is not None and self._info is not None

    def start(self) -> None:
        if self.active:
            return
        session = self.manager.get_public_session()
        addresses = [session.host_ip] + [
            item.address
            for item in self.device_manager.get_local_addresses()
            if item.address != session.host_ip
        ]
        packed_addresses = []
        for address in addresses:
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and not parsed.is_loopback and not parsed.is_global:
                packed_addresses.append(parsed.packed)
        if not packed_addresses:
            raise RuntimeError("No usable IPv4 interface address is available for discovery.")
        service_name = f"{session.session_id}.{SERVICE_TYPE}"
        info = ServiceInfo(
            SERVICE_TYPE,
            service_name,
            port=session.port,
            properties=self._properties(session),
            server=f"sharespace-{session.session_id[:12]}.local.",
            addresses=packed_addresses,
        )
        zeroconf = Zeroconf()
        try:
            zeroconf.register_service(info)
        except Exception:
            zeroconf.close()
            raise
        self._zeroconf = zeroconf
        self._info = info
        self._stop.clear()
        self.error = None
        self._thread = threading.Thread(
            target=self._refresh_loop,
            name="sharespace-mdns-advertiser",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
        zeroconf, info = self._zeroconf, self._info
        self._thread = None
        self._zeroconf = None
        self._info = None
        if zeroconf is None:
            return
        if info is not None:
            try:
                zeroconf.unregister_service(info)
            except Exception:
                logger.exception("Could not withdraw the mDNS ShareSpace service.")
        zeroconf.close()

    def _refresh_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            zeroconf, info = self._zeroconf, self._info
            if zeroconf is None or info is None:
                return
            try:
                session = self.manager.get_public_session()
                refreshed = ServiceInfo(
                    SERVICE_TYPE,
                    info.name,
                    port=session.port,
                    properties=self._properties(session),
                    server=info.server,
                    addresses=info.addresses,
                )
                zeroconf.update_service(refreshed)
                self._info = refreshed
            except Exception as error:
                self.error = str(error)
                logger.warning("ShareSpace mDNS refresh failed: %s", error)
                return

    @staticmethod
    def _properties(session: PublicSession) -> dict[str, bytes]:
        return {
            "protocol": b"1",
            "session_id": session.session_id.encode("ascii"),
            "session_name": _txt_value(session.session_name),
            "host_name": _txt_value(session.host_device_name),
            "host_os": _txt_value(session.host_os, 80),
            "participant_count": str(session.participant_count).encode("ascii"),
            "status": session.status.value.encode("ascii"),
            "created_at": session.created_at.isoformat().encode("ascii"),
        }


@dataclass(frozen=True)
class DiscoveredSession:
    session: PublicSession
    last_seen_monotonic: float
    service_name: str


class _DiscoveryListener(ServiceListener):
    def __init__(self, owner: "SessionDiscovery") -> None:
        self.owner = owner

    def add_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        self.owner._resolve(zeroconf, type_, name)

    def update_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        self.owner._resolve(zeroconf, type_, name)

    def remove_service(self, _zeroconf: Zeroconf, _type: str, name: str) -> None:
        self.owner._remove(name)


class SessionDiscovery:
    """Collect, deduplicate, and expire mDNS session advertisements."""

    def __init__(
        self,
        *,
        stale_after_seconds: int = DISCOVERY_STALE_SECONDS,
        on_change: Callable[[], None] | None = None,
        zeroconf_factory: Callable[[], Zeroconf] = Zeroconf,
    ) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.on_change = on_change
        self._zeroconf_factory = zeroconf_factory
        self._zeroconf: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._lock = threading.Lock()
        self._by_session_id: dict[str, DiscoveredSession] = {}
        self._by_service_name: dict[str, str] = {}
        self._listener = _DiscoveryListener(self)

    @property
    def active(self) -> bool:
        return self._zeroconf is not None

    def start(self) -> None:
        if self.active:
            return
        zeroconf = self._zeroconf_factory()
        try:
            browser = ServiceBrowser(zeroconf, SERVICE_TYPE, listener=self._listener)
        except Exception:
            zeroconf.close()
            raise
        self._zeroconf = zeroconf
        self._browser = browser

    def refresh(self) -> None:
        self.stop(clear=True)
        self.start()

    def stop(self, *, clear: bool = True) -> None:
        browser, zeroconf = self._browser, self._zeroconf
        self._browser = None
        self._zeroconf = None
        if browser is not None:
            browser.cancel()
        if zeroconf is not None:
            zeroconf.close()
        if clear:
            with self._lock:
                self._by_session_id.clear()
                self._by_service_name.clear()
            self._changed()

    def sessions(self, *, now_monotonic: float | None = None) -> list[PublicSession]:
        current = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            expired = [
                session_id
                for session_id, discovered in self._by_session_id.items()
                if current - discovered.last_seen_monotonic > self.stale_after_seconds
            ]
            for session_id in expired:
                discovered = self._by_session_id.pop(session_id)
                self._by_service_name.pop(discovered.service_name, None)
            sessions = [
                item.session.model_copy(deep=True)
                for item in self._by_session_id.values()
                if item.session.status is SessionStatus.ACTIVE
            ]
        if expired:
            self._changed()
        return sorted(sessions, key=lambda item: (item.session_name.casefold(), item.session_id))

    def _resolve(self, zeroconf: Zeroconf, type_: str, name: str) -> None:
        try:
            info = zeroconf.get_service_info(type_, name, timeout=2000)
            if info is None:
                return
            properties = {key.decode("utf-8"): value for key, value in info.properties.items()}
            if self._property(properties, "protocol") != "1":
                return
            session_id = self._property(properties, "session_id")
            status = self._property(properties, "status")
            if not session_id or status != SessionStatus.ACTIVE.value or info.port is None:
                return
            addresses = info.parsed_addresses()
            host_ip = next(
                (address for address in addresses if ipaddress.ip_address(address).version == 4),
                None,
            )
            if host_ip is None:
                return
            created_at_text = self._property(properties, "created_at")
            created_at = datetime.fromisoformat(created_at_text) if created_at_text else datetime.now(timezone.utc)
            public = PublicSession(
                session_id=session_id,
                session_name=self._property(properties, "session_name") or "ShareSpace session",
                host_device_name=self._property(properties, "host_name") or "Nearby device",
                host_os=self._property(properties, "host_os") or "Unknown",
                host_ip=host_ip,
                port=info.port,
                participant_count=max(1, int(self._property(properties, "participant_count") or "1")),
                status=SessionStatus.ACTIVE,
                created_at=created_at,
            )
        except Exception as error:
            logger.debug("Ignored invalid ShareSpace mDNS advertisement %s: %s", name, error)
            return
        with self._lock:
            previous_session = self._by_service_name.get(name)
            if previous_session and previous_session != session_id:
                self._by_session_id.pop(previous_session, None)
            self._by_session_id[session_id] = DiscoveredSession(public, time.monotonic(), name)
            self._by_service_name[name] = session_id
        self._changed()

    def _remove(self, service_name: str) -> None:
        with self._lock:
            session_id = self._by_service_name.pop(service_name, None)
            if session_id is not None:
                discovered = self._by_session_id.get(session_id)
                if discovered is not None and discovered.service_name == service_name:
                    self._by_session_id.pop(session_id, None)
        self._changed()

    @staticmethod
    def _property(properties: dict[str, bytes], key: str) -> str | None:
        value = properties.get(key)
        return value.decode("utf-8", errors="replace") if value is not None else None

    def _changed(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                logger.exception("Discovery change callback failed.")
