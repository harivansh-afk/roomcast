"""Bounded browser interaction for unfamiliar directory sites."""

import asyncio
import hashlib
import time

from .fetch import validate_url
from .subtitles import discover


class Browser:
    def __init__(self, resolver):
        self.resolver = resolver
        self.context = self.page = None
        self.elements = []
        self.streams = {}
        self.created = 0

    async def close(self):
        if self.context:
            await self.context.close()
        self.context = self.page = None
        self.elements = []
        self.streams = {}

    async def act(self, origin, action, element=None, text=None):
        if action not in ("open", "click", "type", "enter", "inspect"):
            raise ValueError("unsupported browser action")
        async with self.resolver.lock, asyncio.timeout(35):
            if action == "open":
                await self.close()
                self.context = await self.resolver.context()
                self.page = await self.context.new_page()
                self.created = time.monotonic()
                self.context.on("response", self.observe)
                await self.page.goto(origin, wait_until="domcontentloaded")
            elif self.page is None or time.monotonic() - self.created > 600:
                await self.close()
                raise ValueError("browser session expired; open a source again")
            elif action != "inspect":
                if type(element) is not int or not 0 <= element < len(self.elements):
                    raise ValueError(
                        "choose an element from the latest browser snapshot"
                    )
                target = self.elements[element]
                if action == "click":
                    await target.click(timeout=5000)
                elif action == "type":
                    if not isinstance(text, str) or len(text) > 200:
                        raise ValueError("text must be at most 200 characters")
                    await target.fill(text, timeout=5000)
                else:
                    await target.press("Enter", timeout=5000)
            await asyncio.sleep(1)
            self.elements = []
            controls = []
            for frame in self.page.frames[:10]:
                for target in await frame.query_selector_all(
                    "a,button,input,textarea,[role=button]"
                ):
                    if len(controls) >= 80:
                        break
                    if not await target.is_visible():
                        continue
                    label = (
                        await target.get_attribute("aria-label")
                        or await target.get_attribute("placeholder")
                        or await target.inner_text()
                    )
                    label = " ".join(label.split())[:120]
                    if label or await target.get_attribute("type") in (
                        "text",
                        "search",
                    ):
                        controls.append({"element": len(self.elements), "label": label})
                        self.elements.append(target)
            return {
                "title": await self.page.title(),
                "controls": controls,
                "streams": [
                    {"id": key, "title": await self.page.title()}
                    for key in self.streams
                ],
            }

    def observe(self, response):
        if response.status != 200 or len(self.streams) >= 32:
            return
        content_type = response.headers.get("content-type", "").split(";")[0].lower()
        if ".m3u8" not in response.url and content_type not in (
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
        ):
            return
        try:
            validate_url(response.url, self.resolver.config.allowed_hosts)
        except ValueError:
            return
        key = hashlib.sha256(response.url.encode()).hexdigest()[:16]
        self.streams[key] = {
            "url": response.url,
            "headers": {
                key: value
                for key, value in response.request.headers.items()
                if key.lower() in ("referer", "origin", "user-agent")
            },
        }
        try:
            self.streams[key]["frame"] = response.request.frame
        except Exception:
            self.streams[key]["frame"] = None

    async def selected(self, key):
        if (
            self.page is None
            or key not in self.streams
            or time.monotonic() - self.created > 600
        ):
            raise ValueError("captured stream expired; browse the source again")
        try:
            yield {
                "title": (await self.page.title())[:180],
                "provider": "browser",
                "sources": [self.streams[key]["url"]],
                "headers": self.streams[key]["headers"],
                "subtitles": {
                    self.streams[key]["url"]: await discover(
                        self.streams[key].get("frame"),
                        self.resolver.config.allowed_hosts,
                    )
                },
            }
        finally:
            await self.close()
