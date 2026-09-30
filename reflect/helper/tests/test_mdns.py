"""The helper's Bonjour advertisement can be discovered and resolved."""

from __future__ import annotations

import asyncio
import socket

import pytest

from reflect_helper.config import SERVICE_TYPE
from reflect_helper.mdns import Advertiser, lan_ipv4_addresses


def test_advertisement_is_discoverable():
    if not lan_ipv4_addresses():
        pytest.skip("no private IPv4 address on this machine")
    asyncio.run(_discover())


async def _discover():
    from zeroconf import IPVersion, ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    advertiser = Advertiser("Reflect Test PC", "abc123", 47999, "windows")
    await advertiser.start()
    browser_zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    found: asyncio.Queue = asyncio.Queue()

    def on_change(zeroconf, service_type, name, state_change):
        if state_change is ServiceStateChange.Added:
            found.put_nowait(name)

    browser = AsyncServiceBrowser(browser_zc.zeroconf, [SERVICE_TYPE], handlers=[on_change])
    try:
        while True:
            name = await asyncio.wait_for(found.get(), 10)
            if name.startswith("Reflect Test PC"):
                break
        info = AsyncServiceInfo(SERVICE_TYPE, name)
        assert await info.async_request(browser_zc.zeroconf, 3000)
        assert info.port == 47999
        props = {k.decode(): v.decode() for k, v in info.properties.items()}
        assert props["id"] == "abc123" and props["os"] == "windows" and props["port"] == "47999"
        addresses = [socket.inet_ntoa(a) for a in info.addresses]
        assert set(addresses) == set(advertiser.addresses)
        assert props["ips"].split(",") == advertiser.addresses
    finally:
        await browser.async_cancel()
        await browser_zc.async_close()
        await advertiser.stop()
