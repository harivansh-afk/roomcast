"""A dedicated browser reads Cinejoy; no personal browser profile or agent code."""
import asyncio
import re
import ipaddress
import socket
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from .fetch import validate_url


class Resolver:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()
        self.driver = self.browser = None

    async def start(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(
            executable_path=self.config.chromium, headless=True,
            chromium_sandbox=True, args=["--disable-dev-shm-usage", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
        )

    async def context(self):
        if self.browser is None:
            await self.start()
        context = await self.browser.new_context(accept_downloads=False, service_workers="block")
        # Prevent sites from probing local services, even though no private profile exists.
        async def route(request):
            url = urlsplit(request.request.url)
            if url.scheme not in ("https", "data", "blob"):
                await request.abort()
                return
            if url.hostname and (url.hostname == "localhost" or re.match(r"^[\d.:]+$", url.hostname)):
                await request.abort()
                return
            if url.hostname:
                try:
                    answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                        url.hostname, 443, type=socket.SOCK_STREAM), timeout=5)
                    if any(not ipaddress.ip_address(answer[4][0]).is_global for answer in answers):
                        await request.abort()
                        return
                except (OSError, asyncio.TimeoutError):
                    await request.abort()
                    return
            await request.continue_()
        await context.route("**/*", route)
        return context

    async def search(self, query):
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 120:
            raise ValueError("query must be 1–120 characters")
        async with self.lock, asyncio.timeout(45):
            context = await self.context()
            try:
                page = await context.new_page()
                await page.goto("https://cinejoy.to/", wait_until="domcontentloaded")
                await page.get_by_role("button", name="Search", exact=True).click()
                field = page.locator('input[type="search"], input[placeholder*="Search" i]').first
                await field.fill(query)
                await page.wait_for_function("""() => [...document.querySelectorAll('a')].some(a =>
                    /\\/(series|movie)\\/\\d+/.test(a.getAttribute('href') || ''))""")
                await page.wait_for_timeout(1500)
                links = await page.locator("a").evaluate_all("els => els.map(a => ({text:a.innerText,href:a.href}))")
                results, seen = [], set()
                for link in links:
                    match = re.search(r"/(series|movie)/(\d+)(?:-|$)", link["href"])
                    if not match or link["href"] in seen:
                        continue
                    if query.casefold() not in link["text"].casefold():
                        continue
                    seen.add(link["href"])
                    results.append({"kind": "tv" if match[1] == "series" else "movie",
                                    "id": int(match[2]), "title": " ".join(link["text"].split())[:180]})
                return results[:20]
            finally:
                await context.close()

    async def resolve(self, kind, content_id, season=1, episode=1):
        if kind not in ("tv", "movie") or type(content_id) is not int or not 0 < content_id < 100000000:
            raise ValueError("invalid content identifier")
        if any(type(n) is not int or not 1 <= n <= 1000 for n in (season, episode)):
            raise ValueError("invalid season or episode")
        path = f"/watch/tv/{content_id}/{season}/{episode}" if kind == "tv" else f"/watch/movie/{content_id}"
        async with self.lock, asyncio.timeout(75):
            context = await self.context()
            try:
                page = await context.new_page()
                candidates = []
                def observe(response):
                    url = response.url
                    if response.status == 200 and (".m3u8" in url or "/m3u8?" in url):
                        try:
                            validate_url(url, self.config.allowed_hosts)
                        except ValueError:
                            return
                        if url not in candidates:
                            candidates.append(url)
                page.on("response", observe)
                await page.goto("https://cinejoy.to" + path, wait_until="domcontentloaded")
                await page.wait_for_selector("video", timeout=30000)
                await page.wait_for_timeout(2000)
                title = (await page.locator("body").inner_text()).splitlines()[0][:180]
                # Nebula supplied standard H.264/AAC in the live compatibility test.
                await page.get_by_role("button", name="Servers", exact=True).click()
                nebula = page.get_by_role("button", name="Nebula", exact=True)
                if await nebula.count():
                    await nebula.click()
                    for _ in range(20):
                        if any(urlsplit(u).hostname == "nebula.bright67.online" for u in candidates):
                            break
                        await asyncio.sleep(0.5)
                if not candidates:
                    raise ValueError("no supported source found; site may have changed")
                candidates.sort(key=lambda u: urlsplit(u).hostname != "nebula.bright67.online")
                # Child playlists are not source alternatives: retain master/entry URLs only.
                candidates = [u for u in candidates if not re.search(r"/(video_\d+p|audio_\d+)\.m3u8", u)]
                if kind == "tv":
                    title += f" — S{season:02d}E{episode:02d}"
                return {"title": title, "sources": candidates[:3], "headers": {
                    "Referer": "https://cinejoy.to/", "Origin": "https://cinejoy.to",
                    "User-Agent": await page.evaluate("navigator.userAgent"),
                }}
            finally:
                await context.close()

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.driver:
            await self.driver.stop()
