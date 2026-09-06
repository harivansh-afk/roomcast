import asyncio
import xml.etree.ElementTree as ET

import aiohttp
from urllib.parse import urlencode
from yarl import URL


class Roku:
    app_id = "782875"
    commands = {"home": "Home", "pause": "Play", "resume": "Play",
                "stop": "Home", "volume_up": "VolumeUp", "volume_down": "VolumeDown",
                "mute": "VolumeMute", "power_on": "PowerOn"}

    def __init__(self, ip, serial):
        self.base = f"http://{ip}:8060"
        self.serial = serial
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), trust_env=False)

    async def request(self, path, params=None):
        async with self.client.request("POST" if params is not None else "GET",
                                       URL(self.base + path + ("?" + urlencode(params) if params else ""), encoded=True),
                                       data=b"" if params is not None else None,
                                       allow_redirects=False) as r:
            if r.status != 200:
                raise ValueError(f"Roku HTTP {r.status}")
            chunks, size = [], 0
            async for chunk in r.content.iter_chunked(8192):
                size += len(chunk)
                if size > 65536:
                    raise ValueError("Roku response too large")
                chunks.append(chunk)
            data = b"".join(chunks)
            return data

    async def verify(self):
        info = ET.fromstring(await self.request("/query/device-info"))
        if info.findtext("serial-number") != self.serial:
            raise ValueError("Roku identity mismatch; refusing to control another device")
        if info.findtext("ecp-setting-mode") != "enabled":
            raise ValueError("Enable Control by mobile apps on Roku")

    async def status(self):
        await self.verify()
        app = ET.fromstring(await self.request("/query/active-app")).find("app")
        player = ET.fromstring(await self.request("/query/media-player"))
        plugin = player.find("plugin")
        def millis(name):
            try:
                return int((player.findtext(name) or "0").split()[0])
            except ValueError:
                return 0
        return {"app": app.text if app is not None else None,
                "app_id": app.get("id") if app is not None else None,
                "player_app_id": plugin.get("id") if plugin is not None else None,
                "state": player.get("state"), "error": player.get("error") == "true",
                "position_ms": millis("position"), "duration_ms": millis("duration")}

    async def launch(self, url, title):
        await self.verify()
        await self.request(f"/launch/{self.app_id}", {
            "u": url, "t": "v", "videoFormat": "hls", "videoName": title,
        })

    async def command(self, command):
        await self.verify()
        if command in ("pause", "resume"):
            state = await self.status()
            if state["app_id"] != self.app_id:
                raise ValueError("Media Assistant is not active")
            if state["state"] != ("play" if command == "pause" else "pause"):
                return state
        await self.request("/keypress/" + self.commands[command], {})
        return await self.status()

    async def confirm(self, seconds=30):
        previous = None
        for _ in range(seconds // 2):
            await asyncio.sleep(2)
            state = await self.status()
            if state["app_id"] == self.app_id and state["player_app_id"] == self.app_id:
                if state["error"]:
                    raise ValueError("Roku rejected the stream")
                if (state["state"] == "play" and previous is not None
                        and state["position_ms"] > previous):
                    return state
                previous = state["position_ms"]
        raise ValueError("Roku did not confirm progressing playback within 30 seconds")

    async def close(self):
        await self.client.close()
