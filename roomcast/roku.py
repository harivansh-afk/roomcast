import asyncio
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import aiohttp
from yarl import URL


class Roku:
    app_id = "782875"
    commands = {
        "home": "Home",
        "pause": "Play",
        "resume": "Play",
        "stop": "Home",
        "volume_up": "VolumeUp",
        "volume_down": "VolumeDown",
        "mute": "VolumeMute",
        "power_on": "PowerOn",
    }

    def __init__(self, ip, serial, app_id="782875"):
        self.base = f"http://{ip}:8060"
        self.serial = serial
        self.app_id = app_id
        self.confirmation = {}
        self.client = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=8), trust_env=False
        )

    async def request(self, path, params=None):
        async with self.client.request(
            "POST" if params is not None else "GET",
            URL(
                self.base + path + ("?" + urlencode(params) if params else ""),
                encoded=True,
            ),
            data=b"" if params is not None else None,
            allow_redirects=False,
        ) as r:
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
            raise ValueError(
                "Roku identity mismatch; refusing to control another device"
            )
        if info.findtext("ecp-setting-mode") != "enabled":
            raise ValueError("Enable Control by mobile apps on Roku")

    async def status(self):
        await self.verify()
        active, media = await asyncio.gather(
            self.request("/query/active-app"), self.request("/query/media-player")
        )
        app = ET.fromstring(active).find("app")
        player = ET.fromstring(media)
        plugin = player.find("plugin")
        media_format = player.find("format")

        def millis(name):
            try:
                return int((player.findtext(name) or "0").split()[0])
            except ValueError:
                return 0

        return {
            "app": app.text if app is not None else None,
            "app_id": app.get("id") if app is not None else None,
            "player_app_id": plugin.get("id") if plugin is not None else None,
            "state": player.get("state"),
            "error": player.get("error") == "true",
            "position_ms": millis("position"),
            "duration_ms": millis("duration"),
            "audio_format": media_format.get("audio")
            if media_format is not None
            else None,
            "video_format": media_format.get("video")
            if media_format is not None
            else None,
            "container": media_format.get("container")
            if media_format is not None
            else None,
            "caption_format": media_format.get("captions")
            if media_format is not None
            else None,
        }

    @staticmethod
    def has_av(state):
        return all(
            state.get(key) not in (None, "", "none", "unknown")
            for key in ("audio_format", "video_format")
        )

    async def launch(
        self, url, title, start_seconds=0, subtitles=None, report_url=None
    ):
        await self.verify()
        await self.request(
            f"/launch/{self.app_id}",
            {
                "u": url,
                "t": "v",
                "videoFormat": "hls",
                "videoName": title,
                **({"startSeconds": start_seconds} if start_seconds else {}),
                **(subtitles or {}),
                **({"playbackReportUrl": report_url} if report_url else {}),
            },
        )

    async def subtitles(self, params):
        await self.verify()
        await self.request("/input", {"a": "subtitles", **params})

    async def launch_youtube(self, video_id):
        await self.verify()
        await self.request(
            "/launch/837", {"contentId": video_id, "mediaType": "shortFormVideo"}
        )

    async def command(self, command):
        if command in ("pause", "resume"):
            state = await self.status()
            if state["app_id"] not in (self.app_id, "837"):
                raise ValueError("No supported playback app is active")
            if state["state"] != ("play" if command == "pause" else "pause"):
                return state
        else:
            await self.verify()
        await self.request("/keypress/" + self.commands[command], {})
        return await self.status()

    async def confirm(self, seconds=30, app_id=None, delivered=None, start_seconds=0):
        app_id = app_id or self.app_id
        previous = None
        progressing = 0
        first_position = None
        started = time.monotonic()
        self.confirmation = {}
        try:
            async with asyncio.timeout(seconds):
                while True:
                    state = await self.status()
                    if state["app_id"] == app_id and state["player_app_id"] == app_id:
                        if state["error"]:
                            raise ValueError("Roku rejected the stream")
                        healthy = (
                            state["state"] == "play"
                            and self.has_av(state)
                            and (delivered is None or delivered())
                        )
                        if healthy and "first_av_seconds" not in self.confirmation:
                            self.confirmation["first_av_seconds"] = round(
                                time.monotonic() - started, 3
                            )
                        if start_seconds and first_position is None and healthy:
                            first_position = state["position_ms"] / 1000
                            if (
                                not start_seconds - 2
                                <= first_position
                                <= start_seconds + time.monotonic() - started + 2
                            ):
                                raise ValueError(
                                    "Roku did not start at the requested timestamp"
                                )
                        if (
                            healthy
                            and previous is not None
                            and state["position_ms"] > previous
                        ):
                            progressing += 1
                            if progressing >= 2:
                                return state
                        else:
                            progressing = 0
                        previous = state["position_ms"] if healthy else None
                    else:
                        previous, progressing, first_position = None, 0, None
                    await asyncio.sleep(0.25)
        except TimeoutError:
            raise ValueError(
                "Roku did not confirm progressing audio and video before startup timeout"
            ) from None

    async def seek_to(self, target, before):
        await self.verify()
        started = time.monotonic()
        await self.request(
            "/input",
            {
                "a": "seek",
                "positionSeconds": target,
                "stayPaused": "true" if before["state"] == "pause" else "false",
            },
        )
        return await self.confirm_seek(target, before, started=started)

    async def confirm_seek(self, target, before, seconds=30, started=None):
        started = time.monotonic() if started is None else started
        initial = before["position_ms"] / 1000
        paused = before["state"] == "pause"
        app_id = before["app_id"]
        first = None
        try:
            async with asyncio.timeout(seconds):
                while True:
                    await asyncio.sleep(0.25)
                    state = await self.status()
                    if state["app_id"] != app_id or state.get("error"):
                        raise ValueError(
                            "TV app changed or reported an error during seek"
                        )
                    if state["player_app_id"] != app_id or not self.has_av(state):
                        continue
                    if state["state"] != ("pause" if paused else "play"):
                        continue
                    position = state["position_ms"] / 1000
                    if first is None:
                        # Natural progression through the target is not proof that
                        # an ignored seek command worked.
                        natural = initial + (
                            0 if paused else time.monotonic() - started
                        )
                        if abs(position - natural) <= 2 and abs(initial - target) > 2:
                            continue
                        if abs(position - target) > 2:
                            continue
                        first = position, time.monotonic()
                    elif paused or position > first[0]:
                        expected = first[0] + (
                            0 if paused else time.monotonic() - first[1]
                        )
                        if abs(position - expected) <= 2:
                            return {
                                "confirmed": True,
                                "target_seconds": target,
                                "actual_seconds": position,
                                "tolerance_seconds": 2,
                                "roku": state,
                            }
        except TimeoutError:
            raise ValueError(
                "Roku did not confirm the requested seek position"
            ) from None

    async def close(self):
        await self.client.close()
