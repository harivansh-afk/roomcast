"""A dedicated browser reads Cinejoy; no personal browser profile or agent code."""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlencode, urlsplit

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from .fetch import validate_url
from .subtitles import discover


class Resolver:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.driver = self.browser = None

    async def start(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(
            executable_path=self.config.chromium,
            headless=True,
            chromium_sandbox=True,
            args=[
                "--disable-dev-shm-usage",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        )

    async def context(self, lightweight=False):
        if self.browser is None:
            await self.start()
        context = await self.browser.new_context(
            accept_downloads=False, service_workers="block"
        )

        # Prevent sites from probing local services, even though no private profile exists.
        async def route(request):
            if lightweight and request.request.resource_type in ("image", "font"):
                await request.abort()
                return
            url = urlsplit(request.request.url)
            if url.scheme not in ("https", "data", "blob"):
                await request.abort()
                return
            if url.hostname and (
                url.hostname == "localhost" or re.match(r"^[\d.:]+$", url.hostname)
            ):
                await request.abort()
                return
            if url.hostname:
                try:
                    answers = await asyncio.wait_for(
                        asyncio.get_running_loop().getaddrinfo(
                            url.hostname, 443, type=socket.SOCK_STREAM
                        ),
                        timeout=5,
                    )
                    if any(
                        not ipaddress.ip_address(answer[4][0]).is_global
                        for answer in answers
                    ):
                        await request.abort()
                        return
                except (OSError, asyncio.TimeoutError):
                    await request.abort()
                    return
            await request.continue_()

        await context.route("**/*", route)
        return context

    async def search(self, query, origin=None):
        origin = origin or self.config.site_url
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise ValueError("query must be 1–120 characters")
        async with self.lock, asyncio.timeout(45):
            context = await self.context(lightweight=True)
            try:
                page = await context.new_page()
                await page.goto(origin + "/", wait_until="domcontentloaded")
                await page.get_by_role("button", name="Search", exact=True).click()
                field = page.locator(
                    'input[type="search"], input[placeholder*="Search" i]'
                ).first
                await field.fill(query)
                await page.wait_for_function("""() => [...document.querySelectorAll('a')].some(a =>
                    /\\/(series|movie)\\/\\d+/.test(a.getAttribute('href') || ''))""")
                await page.wait_for_timeout(1500)
                links = await page.locator("a").evaluate_all(
                    "els => els.map(a => ({text:a.innerText,href:a.href}))"
                )
                results, seen = [], set()
                for link in links:
                    match = re.search(r"/(series|movie)/(\d+)(?:-|$)", link["href"])
                    if not match or link["href"] in seen:
                        continue
                    if query.casefold() not in link["text"].casefold():
                        continue
                    seen.add(link["href"])
                    results.append(
                        {
                            "kind": "tv" if match[1] == "series" else "movie",
                            "id": int(match[2]),
                            "title": " ".join(link["text"].split())[:180],
                        }
                    )
                return results[:20]
            finally:
                await context.close()

    async def search_youtube(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise ValueError("query must be 1–120 characters")
        async with self.lock, asyncio.timeout(45):
            context = await self.context(lightweight=True)
            try:
                page = await context.new_page()
                await page.goto(
                    "https://www.youtube.com/results?"
                    + urlencode({"search_query": query}),
                    wait_until="domcontentloaded",
                )
                links = page.locator("a#video-title")
                await links.first.wait_for()
                rows = await links.evaluate_all(
                    "els => els.map(e => ({title:e.textContent.trim(),url:e.href}))"
                )
                results = []
                seen = set()
                for row in rows:
                    match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})(?:&|$)", row["url"])
                    if match and match[1] not in seen:
                        seen.add(match[1])
                        results.append(
                            {
                                "kind": "youtube",
                                "id": match[1],
                                "title": row["title"][:180],
                            }
                        )
                return results[:10]
            finally:
                await context.close()

    async def resolve(self, kind, content_id, season=1, episode=1, origin=None):
        """Yield fresh candidates lazily, advancing servers only after consumer failure."""
        origin = origin or self.config.site_url
        path = (
            f"/watch/tv/{content_id}/{season}/{episode}"
            if kind == "tv"
            else f"/watch/movie/{content_id}"
        )
        async with self.lock:
            context = await self.context(lightweight=True)
            try:
                page = await context.new_page()
                candidates = []
                candidate_frames = {}
                ready = asyncio.Event()

                def observe(response):
                    url = response.url
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].lower()
                    )
                    if response.status == 200 and (
                        ".m3u8" in url
                        or content_type
                        in ("application/vnd.apple.mpegurl", "application/x-mpegurl")
                    ):
                        try:
                            validate_url(url, self.config.allowed_hosts)
                        except ValueError:
                            return
                        if url not in candidates:
                            candidates.append(url)
                            ready.set()
                            try:
                                candidate_frames[url] = response.request.frame
                            except Exception:
                                candidate_frames[url] = None

                page.on("response", observe)
                await page.goto(origin + path, wait_until="domcontentloaded")
                await page.get_by_role("button", name="Servers", exact=True).click()
                rows = page.locator("button.src-row")
                await rows.first.wait_for()
                providers = [name.strip() for name in await rows.all_text_contents()][
                    :8
                ]
                title = (await page.locator("body").inner_text()).splitlines()[0][:180]
                if kind == "tv":
                    title += f" — S{season:02d}E{episode:02d}"
                headers = {
                    "Referer": origin + "/",
                    "Origin": origin,
                    "User-Agent": await page.evaluate("navigator.userAgent"),
                }
                for index, provider in enumerate(providers):
                    try:
                        if index:
                            if not await rows.first.is_visible():
                                await page.get_by_role(
                                    "button", name="Servers", exact=True
                                ).click()
                            candidates.clear()
                            candidate_frames.clear()
                            ready.clear()
                        await page.get_by_role(
                            "button", name=provider, exact=True
                        ).click(timeout=5000)
                        try:
                            await asyncio.wait_for(ready.wait(), 10)
                        except TimeoutError:
                            continue
                        if candidates:
                            yield {
                                "title": title,
                                "provider": provider,
                                "sources": list(candidates[:3]),
                                "headers": headers,
                                "subtitles": {
                                    url: await discover(
                                        candidate_frames.get(url),
                                        self.config.allowed_hosts,
                                    )
                                    for url in candidates[:3]
                                },
                            }
                    except PlaywrightTimeoutError:
                        continue
            finally:
                await context.close()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.driver:
            await self.driver.stop()
