"""Bonjour/mDNS advertisement as _reflect._tcp.local. so the phone finds the helper."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket

from .config import SERVICE_TYPE

log = logging.getLogger(__name__)

RECHECK_SECONDS = 30


def primary_ipv4() -> str | None:
    """Address of the interface holding the default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere
            return s.getsockname()[0]
    except OSError:
        return None


def lan_ipv4_addresses() -> list[str]:
    import ifaddr

    found: list[str] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue
            addr = ipaddress.IPv4Address(ip.ip)
            if addr.is_private and not addr.is_loopback and not addr.is_link_local and str(addr) not in found:
                found.append(str(addr))
    primary = primary_ipv4()
    if primary in found:
        found.remove(primary)
        found.insert(0, primary)
    return found


def _host_label(name: str) -> str:
    label = re.sub(r"[^A-Za-z0-9-]+", "-", name).strip("-").lower() or "computer"
    return f"{label[:40]}-reflect"


def _instance_name(name: str) -> str:
    encoded = name.encode("utf-8")[:60]
    return encoded.decode("utf-8", "ignore") or "Reflect"


class Advertiser:
    def __init__(self, name: str, helper_id: str, port: int, os_name: str):
        self.name = name
        self.helper_id = helper_id
        self.port = port
        self.os_name = os_name
        self.addresses: list[str] = []
        self._zc = None
        self._info = None
        self._task: asyncio.Task | None = None

    def _service_info(self, addresses: list[str]):
        from zeroconf import ServiceInfo

        instance = self._info.name if self._info is not None else f"{_instance_name(self.name)}.{SERVICE_TYPE}"
        return ServiceInfo(
            SERVICE_TYPE,
            instance,
            addresses=[socket.inet_aton(a) for a in addresses],
            port=self.port,
            properties={
                "id": self.helper_id,
                "v": "1",
                "os": self.os_name,
                "name": self.name,
                "port": str(self.port),
                "ips": ",".join(addresses),
            },
            server=f"{_host_label(self.name)}.local.",
        )

    async def start(self) -> None:
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncZeroconf

        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        await self._register(lan_ipv4_addresses())
        self._task = asyncio.create_task(self._watch_addresses())

    async def _register(self, addresses: list[str]) -> None:
        self.addresses = addresses
        if not addresses:
            log.warning("No local network address found; the phone cannot discover this computer yet")
            return
        info = self._service_info(addresses)
        if self._info is None:
            await self._zc.async_register_service(info, allow_name_change=True)
            log.info("Advertising %r on %s port %d", info.name, ", ".join(addresses), self.port)
        else:
            await self._zc.async_update_service(info)
            log.info("Network changed; now advertising on %s", ", ".join(addresses))
        self._info = info

    async def _watch_addresses(self) -> None:
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            try:
                addresses = lan_ipv4_addresses()
                if addresses != self.addresses:
                    await self._register(addresses)
            except Exception:
                log.exception("mDNS address refresh failed")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._zc is not None:
            try:
                if self._info is not None:
                    await self._zc.async_unregister_service(self._info)
            finally:
                await self._zc.async_close()
            self._zc = None
