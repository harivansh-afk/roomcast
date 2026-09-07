"""Locate one paired Roku; DHCP addresses are hints, never device identity."""

import asyncio
import ipaddress
import json
import socket
from pathlib import Path

import aiohttp


class Network:
    def __init__(self, config, roku):
        self.config, self.roku = config, roku
        self.lock = asyncio.Lock()
        self.address = None
        self.local_address = None

    async def route(self, address):
        process = await asyncio.create_subprocess_exec(
            self.config.ip_command,
            "-j",
            "-4",
            "route",
            "get",
            address,
            stdout=asyncio.subprocess.PIPE,
        )
        output, _ = await asyncio.wait_for(process.communicate(), 3)
        routes = json.loads(output)
        if not routes or routes[0].get("dev") != self.config.lan_interface:
            raise ValueError("TV is not reachable through the configured LAN interface")
        return routes[0]["prefsrc"]

    async def probe(self, address):
        import xml.etree.ElementTree as ET

        try:
            ip = ipaddress.IPv4Address(address)
            if not ip.is_private or ip.is_loopback or ip.is_unspecified:
                return False
            async with self.roku.client.get(
                f"http://{ip}:8060/query/device-info",
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=1),
            ) as response:
                if response.status != 200:
                    return False
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    data.extend(chunk)
                    if len(data) > 65536:
                        return False
                return (
                    ET.fromstring(data).findtext("serial-number")
                    == self.config.roku_serial
                )
        except (
            ValueError,
            OSError,
            aiohttp.ClientError,
            asyncio.TimeoutError,
            ET.ParseError,
        ):
            return False

    async def ssdp(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        found = []
        try:
            sock.bind(("0.0.0.0", self.config.discovery_port))
            await loop.sock_sendto(
                sock,
                b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: roku:ecp\r\n\r\n',
                ("239.255.255.250", 1900),
            )
            async with asyncio.timeout(2):
                while len(found) < 8:
                    data, peer = await loop.sock_recvfrom(sock, 8192)
                    if ("roku:ecp:" + self.config.roku_serial).encode() in data:
                        found.append(peer[0])
        except (TimeoutError, OSError):
            pass
        finally:
            sock.close()
        return found

    async def select(self, address):
        local = await self.route(address)
        if (self.address, self.local_address) == (address, local):
            return address
        self.address, self.local_address = address, local
        self.roku.base = f"http://{address}:8060"
        path = Path(self.config.address_cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"serial": self.config.roku_serial, "address": address})
        )
        temporary.replace(path)
        return address

    async def ensure(self):
        async with self.lock:
            candidates = [self.address, self.config.roku_ip]
            try:
                cached = json.loads(Path(self.config.address_cache).read_text())
                if cached.get("serial") == self.config.roku_serial:
                    candidates.insert(1, cached.get("address"))
            except (OSError, ValueError):
                pass
            if self.config.roku_mac:
                for line in Path("/proc/net/arp").read_text().splitlines()[1:]:
                    fields = line.split()
                    if (
                        len(fields) >= 6
                        and fields[3].lower() == self.config.roku_mac.lower()
                        and fields[5] == self.config.lan_interface
                    ):
                        candidates.insert(1, fields[0])
            for address in dict.fromkeys(a for a in candidates if a):
                if await self.probe(address):
                    return await self.select(address)
            for address in await self.ssdp():
                if await self.probe(address):
                    return await self.select(address)
            # A bounded, administrator-configured fallback when multicast is filtered.
            addresses = [
                str(ip)
                for network in self.config.discovery_networks
                for ip in ipaddress.IPv4Network(network).hosts()
            ]
            for start in range(0, len(addresses), 16):
                batch = addresses[start : start + 16]
                results = await asyncio.gather(
                    *(self.probe(address) for address in batch)
                )
                for address, matches in zip(batch, results):
                    if matches:
                        return await self.select(address)
            self.address = self.local_address = None
            raise ValueError("Paired Roku not found; check TV power and LAN isolation")
