import asyncio
import hashlib
import re
import secrets
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from .fetch import validate_url


@dataclass(frozen=True)
class Resource:
    url: str
    init: str | None = None


class Session:
    def __init__(self, fetcher, config, source, headers, title):
        validate_url(source, config.allowed_hosts)
        self.fetcher, self.config = fetcher, config
        self.headers, self.title = headers, title
        self.token = secrets.token_urlsafe(24)
        self.created = time.monotonic()
        self.resources = {}
        self.cache = OrderedDict()
        self.cache_size = 0
        self.pending = {}
        self.work = asyncio.Semaphore(4)
        self.encoders = asyncio.Semaphore(2)
        self.tasks = set()
        self.closed = False
        self.metrics = {"upstream_bytes": 0, "cache_hits": 0, "remux_seconds": 0.0,
                        "segments_remuxed": 0, "fetch_seconds": 0.0}
        self.root = self.register(source)

    def register(self, url, init=None):
        validate_url(url, self.config.allowed_hosts)
        if init:
            validate_url(init, self.config.allowed_hosts)
        key = hashlib.sha256((url + "\0" + (init or "")).encode()).hexdigest()[:24]
        if len(self.resources) >= 10000 and key not in self.resources:
            raise ValueError("playlist resource limit exceeded")
        self.resources[key] = Resource(url, init)
        return key

    def link(self, key):
        return f"{self.config.public_base.rstrip('/')}/media/{self.token}/{key}"

    def put(self, key, value):
        if key in self.cache:
            self.cache_size -= len(self.cache.pop(key)[0])
        self.cache[key] = value
        self.cache_size += len(value[0])
        while self.cache_size > self.config.cache_bytes and self.cache:
            _, old = self.cache.popitem(last=False)
            self.cache_size -= len(old[0])

    async def raw(self, url):
        key = ("raw", url)
        if key in self.cache:
            self.cache.move_to_end(key)
            self.metrics["cache_hits"] += 1
            return self.cache[key]
        start = time.monotonic()
        data, final = await self.fetcher.get(url, self.headers)
        self.metrics["fetch_seconds"] += time.monotonic() - start
        self.metrics["upstream_bytes"] += len(data)
        self.put(key, (data, final))
        return data, final

    def rewrite(self, body, base):
        lines = body.decode("utf-8-sig").splitlines()
        if not lines or lines[0] != "#EXTM3U":
            raise ValueError("invalid HLS playlist")
        if any(line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:", "#EXT-X-BYTERANGE:")) for line in lines):
            raise ValueError("encrypted or byte-range HLS is not supported")
        if "#EXT-X-ENDLIST" not in lines and any(line.startswith("#EXTINF:") for line in lines):
            raise ValueError("only complete on-demand playlists are supported")
        variants = []
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:"):
                match = re.search(r"RESOLUTION=\d+x(\d+)", line)
                if index + 1 >= len(lines) or lines[index + 1].startswith("#"):
                    raise ValueError("invalid variant URI")
                variants.append((int(match[1]) if match else 0, index))
        chosen = None
        if variants:
            eligible = [v for v in variants if v[0] <= self.config.max_height]
            if not eligible:
                raise ValueError("no variant within configured resolution limit")
            chosen = max(eligible)[1]
        skipped = {i for _, i in variants if i != chosen}
        init = None
        output, segments = [], []
        for index, line in enumerate(lines):
            if index in skipped or index - 1 in skipped:
                continue
            if line.startswith("#EXT-X-MAP:"):
                match = re.search(r'URI="([^"]+)"', line)
                if not match or "BYTERANGE=" in line:
                    raise ValueError("invalid initialization segment")
                init = urljoin(base, match[1])
                validate_url(init, self.config.allowed_hosts)
                continue
            if line and not line.startswith("#"):
                key = self.register(urljoin(base, line), init)
                output.append(self.link(key))
                if not variants:
                    segments.append(key)
            elif 'URI="' in line:
                def replace(match):
                    return 'URI="' + self.link(self.register(urljoin(base, match[1]))) + '"'
                output.append(re.sub(r'URI="([^"]+)"', replace, line))
            else:
                output.append(line)
        return ("\n".join(output) + "\n").encode(), segments

    async def remux(self, data, init):
        async with self.encoders:
            start = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="roomcast-") as directory:
                src, dest = Path(directory) / "input.mp4", Path(directory) / "segment.ts"
                src.write_bytes(init + data)
                process = await asyncio.create_subprocess_exec(
                    self.config.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-protocol_whitelist", "file", "-copyts", "-i", str(src),
                    "-map", "0:v?", "-map", "0:a?", "-c", "copy",
                    "-mpegts_copyts", "1", "-muxdelay", "0", "-muxpreload", "0",
                    "-f", "mpegts", str(dest), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
                if process.returncode or not dest.exists() or dest.stat().st_size > 40 * 1024 * 1024:
                    raise ValueError("segment repackaging failed")
                result = dest.read_bytes()
            self.metrics["segments_remuxed"] += 1
            self.metrics["remux_seconds"] += time.monotonic() - start
            return result

    async def _load(self, key):
        async with self.work:
            resource = self.resources[key]
            data, final = await self.raw(resource.url)
            if data.startswith((b"#EXTM3U", b"\xef\xbb\xbf#EXTM3U")):
                data, segments = self.rewrite(data, final)
                result = data, "application/vnd.apple.mpegurl"
                for upcoming in segments[:2]:
                    task = asyncio.create_task(self._prefetch(upcoming))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
            elif resource.init:
                init, _ = await self.raw(resource.init)
                result = await self.remux(data, init), "video/mp2t"
            else:
                result = data, "application/octet-stream"
            self.put(key, result)
            return result

    async def _prefetch(self, key):
        try:
            await self.get(key)
        except Exception:
            pass

    async def get(self, key):
        if self.closed or time.monotonic() - self.created > self.config.session_seconds:
            raise ValueError("playback session expired; resolve the source again")
        if key not in self.resources:
            raise KeyError(key)
        if key in self.cache:
            self.cache.move_to_end(key)
            self.metrics["cache_hits"] += 1
            return self.cache[key]
        if key not in self.pending:
            task = asyncio.create_task(self._load(key))
            self.pending[key] = task
            task.add_done_callback(lambda done: self.pending.pop(key, None))
        return await asyncio.shield(self.pending[key])

    async def close(self):
        self.closed = True
        tasks = list(self.tasks) + list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.cache.clear()
        self.cache_size = 0
