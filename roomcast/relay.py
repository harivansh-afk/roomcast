import asyncio
import hashlib
import math
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
    role: str | None = None
    duration: float = 0


class Session:
    def __init__(self, fetcher, config, source, headers, title, start_seconds=0):
        validate_url(source, config.allowed_hosts)
        self.fetcher, self.config = fetcher, config
        self.headers, self.title = headers, title
        self.token = secrets.token_urlsafe(24)
        self.created = time.monotonic()
        self.resources = {}
        self.cache = OrderedDict()
        self.cache_size = 0
        self.pending = {}
        self.raw_pending = {}
        self.prefetches = {}
        self.prefetch_slots = asyncio.Semaphore(2)
        self.following = {}
        self.selected_playlists = []
        self.start_seconds = start_seconds
        self.work = asyncio.Semaphore(4)
        self.remux_slots = asyncio.Semaphore(2)
        self.tasks = set()
        self.closed = False
        self.metrics = {
            "upstream_bytes": 0,
            "cache_hits": 0,
            "remux_seconds": 0.0,
            "segments_remuxed": 0,
            "fetch_seconds": 0.0,
            "validation_seconds": 0.0,
        }
        self.selections = {}
        self.audio_selections = {}
        self.subtitle_tracks = []
        self.subtitle_error = None
        self.subtitle_request = None
        self.subtitle_report = None
        self.player_report = None
        self.subtitle_event = asyncio.Event()
        self.roles = {}
        self.evidence = {}
        self.checked_inits = set()
        self.delivered = set()
        self.failure = None
        self.playlists = {}
        self.preflight = {"state": "unchecked", "visual_verified": False}
        self.root = self.register(source)

    async def prepare(self):
        from .preflight import prepare

        task = asyncio.create_task(prepare(self))
        self.tasks.add(task)
        try:
            await task
            return self.preflight
        finally:
            self.tasks.discard(task)

    def register(self, url, init=None, role=None, duration=0):
        validate_url(url, self.config.allowed_hosts)
        if init:
            validate_url(init, self.config.allowed_hosts)
        key = hashlib.sha256(repr((url, init, role, duration)).encode()).hexdigest()[
            :24
        ]
        if role == "subtitle-file":
            key += ".srt"
        if len(self.resources) >= 10000 and key not in self.resources:
            raise ValueError("playlist resource limit exceeded")
        self.resources[key] = Resource(url, init, role, duration)
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

    def pin_playlist(self, url, body, final):
        if (
            sum(len(v[0]) for k, v in self.playlists.items() if k != url) + len(body)
            > 2 * 1024 * 1024
        ):
            raise ValueError("pinned playlist budget exceeded")
        self.playlists[url] = (body, final)

    @staticmethod
    def forget(pending, key, task):
        if pending.get(key) is task:
            pending.pop(key)
        # A cancelled waiter can leave a shared job without another waiter.
        # Retrieve its exception; callers still receive the original failure.
        if not task.cancelled():
            task.exception()

    async def raw(self, url, limit=None, *, cache=True):
        if url in self.playlists:
            result = self.playlists[url]
            if limit and len(result[0]) > limit:
                raise ValueError("upstream object exceeds limit")
            return result
        key = ("raw", url)
        if key in self.cache:
            self.cache.move_to_end(key)
            self.metrics["cache_hits"] += 1
            result = self.cache[key]
            if limit and len(result[0]) > limit:
                raise ValueError("upstream object exceeds limit")
            return result
        # Init objects can be requested by several segments simultaneously.
        # Coalesce those downloads, including the resource-less preflight reads.
        if url not in self.raw_pending:

            async def fetch():
                start = time.monotonic()
                result = await self.fetcher.get(
                    url, self.headers, **({"limit": limit} if limit else {})
                )
                self.metrics["fetch_seconds"] += time.monotonic() - start
                self.metrics["upstream_bytes"] += len(result[0])
                return result

            task = asyncio.create_task(fetch())
            self.raw_pending[url] = task
            task.add_done_callback(
                lambda done: self.forget(self.raw_pending, url, done)
            )
        data, final = await asyncio.shield(self.raw_pending[url])
        if limit and len(data) > limit:
            raise ValueError("upstream object exceeds limit")
        if cache:
            self.put(key, (data, final))
        return data, final

    def rewrite(self, body, base):
        lines = body.decode("utf-8-sig").splitlines()
        if not lines or lines[0] != "#EXTM3U":
            raise ValueError("invalid HLS playlist")
        if any(
            line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:", "#EXT-X-BYTERANGE:"))
            for line in lines
        ):
            raise ValueError("encrypted or byte-range HLS is not supported")
        if "#EXT-X-ENDLIST" not in lines and any(
            line.startswith("#EXTINF:") for line in lines
        ):
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
            chosen = self.selections.get(base)
            if chosen not in {i for _, i in variants}:
                raise ValueError("master requires compatibility preflight")
        skipped = {i for _, i in variants if i != chosen}
        audio_group = None
        subtitle_group = None
        if chosen is not None:
            from .preflight import attributes, value

            audio_group = value(attributes(lines[chosen]), "AUDIO")
            subtitle_group = value(attributes(lines[chosen]), "SUBTITLES")
            self.subtitle_tracks = [
                t for t in self.subtitle_tracks if t["kind"] != "hls"
            ]
        init, duration = None, None
        role = self.roles.get(base)
        output, segments = [], []
        for index, line in enumerate(lines):
            if index in skipped or index - 1 in skipped:
                continue
            if variants and line.startswith("#EXT-X-MEDIA:"):
                attrs = attributes(line)
                if (
                    value(attrs, "TYPE") == "SUBTITLES"
                    and value(attrs, "GROUP-ID") == subtitle_group
                ):
                    from .subtitles import track

                    try:
                        item = track(
                            urljoin(base, value(attrs, "URI")),
                            value(attrs, "NAME"),
                            value(attrs, "LANGUAGE"),
                            "hls",
                            self.config.allowed_hosts,
                            value(attrs, "DEFAULT") == "YES",
                            value(attrs, "FORCED") == "YES",
                        )
                        if (
                            not value(attrs, "URI")
                            or sum(t["kind"] == "hls" for t in self.subtitle_tracks)
                            >= 32
                        ):
                            continue
                        key = self.register(item["url"], role="subtitle")
                    except ValueError:
                        self.subtitle_error = "A subtitle track has an unsupported URL"
                        continue
                    self.subtitle_tracks.append(item)
                    self.roles[item["url"]] = "subtitle"
                    attrs["URI"] = '"' + self.link(key) + '"'
                    attrs["NAME"] = '"' + item["name"] + '"'
                    output.append(
                        "#EXT-X-MEDIA:" + ",".join(f"{k}={v}" for k, v in attrs.items())
                    )
                    continue
                elif (
                    value(attrs, "TYPE") != "AUDIO"
                    or value(attrs, "GROUP-ID") != audio_group
                    or index != self.audio_selections.get(base)
                ):
                    continue
            if line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
                continue
            if line.startswith("#EXT-X-MAP:"):
                if role == "subtitle":
                    raise ValueError(
                        "only plain WebVTT subtitle segments are supported"
                    )
                match = re.search(r'URI="([^"]+)"', line)
                if not match or "BYTERANGE=" in line:
                    raise ValueError("invalid initialization segment")
                init = urljoin(base, match[1])
                validate_url(init, self.config.allowed_hosts)
                if role != "muxed":
                    output.append(
                        '#EXT-X-MAP:URI="' + self.link(self.register(init)) + '"'
                    )
                continue
            if line.startswith("#EXTINF:"):
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
                maximum = 21600 if role == "subtitle" else 30
                if not math.isfinite(duration) or not 0 < duration <= maximum:
                    raise ValueError(
                        f"HLS segment duration must be within {maximum} seconds"
                    )
            if line and not line.startswith("#"):
                if not variants and duration is None:
                    raise ValueError("media segment has no duration")
                key = self.register(urljoin(base, line), init, role, duration or 0)
                duration = None
                output.append(self.link(key))
                if not variants:
                    segments.append(key)
            elif 'URI="' in line:

                def replace(match):
                    return (
                        'URI="'
                        + self.link(self.register(urljoin(base, match[1])))
                        + '"'
                    )

                output.append(re.sub(r'URI="([^"]+)"', replace, line))
            else:
                if line.startswith("#EXT-X-STREAM-INF:"):
                    line = re.sub(r',?CLOSED-CAPTIONS=("[^"]*"|[^,]*)', "", line)
                output.append(line)
        if variants and not any(t["kind"] == "hls" for t in self.subtitle_tracks):
            output = [
                re.sub(r',?SUBTITLES="[^"]*"', "", line)
                if line.startswith("#EXT-X-STREAM-INF:")
                else line
                for line in output
            ]
        for index, key in enumerate(segments):
            self.following[key] = segments[index + 1 : index + 3]
        return ("\n".join(output) + "\n").encode(), segments

    def at_position(self, segments, position):
        offset = 0.0
        for key in segments:
            duration = self.resources[key].duration
            if offset + duration > position:
                return key, offset
            offset += duration
        return segments[-1], offset - self.resources[segments[-1]].duration

    def prime(self, position):
        for segments in self.selected_playlists:
            key, _ = self.at_position(segments, position)
            self.prefetch([key, *self.following[key]])

    def prefetch(self, keys):
        # At most four queued, two running. Demand has two free work slots.
        # Prefetch is driven by the playhead, never recursively by a fetch.
        for key in keys:
            if self.closed or len(self.prefetches) >= 4:
                break
            if key in self.cache or key in self.pending or key in self.prefetches:
                continue
            task = asyncio.create_task(self._prefetch(key))
            self.prefetches[key] = task
            task.add_done_callback(lambda done, key=key: self.prefetches.pop(key, None))

    async def remux(self, data, init):
        async with self.remux_slots:
            start = time.monotonic()
            with tempfile.TemporaryDirectory(prefix="roomcast-") as directory:
                src, dest = (
                    Path(directory) / "input.mp4",
                    Path(directory) / "segment.ts",
                )
                src.write_bytes(init + data)
                process = await asyncio.create_subprocess_exec(
                    self.config.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    "mov,mpegts,aac,ac3,eac3,mp3",
                    "-copyts",
                    "-i",
                    str(src),
                    "-map",
                    "0:v?",
                    "-map",
                    "0:a?",
                    "-c",
                    "copy",
                    "-mpegts_copyts",
                    "1",
                    "-muxdelay",
                    "0",
                    "-muxpreload",
                    "0",
                    "-fs",
                    "41943040",
                    "-f",
                    "mpegts",
                    str(dest),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()
                    raise
                if (
                    process.returncode
                    or not dest.exists()
                    or not 0 < dest.stat().st_size < 40 * 1024 * 1024
                ):
                    raise ValueError("segment repackaging failed")
                result = dest.read_bytes()
            self.metrics["segments_remuxed"] += 1
            self.metrics["remux_seconds"] += time.monotonic() - start
            return result

    async def _load(self, key):
        async with self.work:
            resource = self.resources[key]
            from .subtitles import MAX_SUBTITLE_BYTES, subtitle_text

            is_subtitle = resource.role in ("subtitle", "subtitle-file")
            init = b""
            if resource.init:
                init, init_final = await self.raw(resource.init)
                check = (resource.init, resource.role)
                if resource.role and check not in self.checked_inits:
                    from .preflight import inspect_init

                    await inspect_init(init, self.config, resource.role)
                    self.checked_inits.add(check)
                self.pin_playlist(resource.init, init, init_final)
            data, final = await self.raw(
                resource.url,
                MAX_SUBTITLE_BYTES if is_subtitle else None,
                cache=not resource.duration,
            )
            if data.startswith((b"#EXTM3U", b"\xef\xbb\xbf#EXTM3U")):
                if resource.role == "subtitle-file":
                    raise ValueError("expected a subtitle file, not a playlist")
                if resource.role == "subtitle":
                    self.roles[final] = "subtitle"
                data, segments = self.rewrite(data, final)
                result = data, "application/vnd.apple.mpegurl"
            elif is_subtitle:
                result = subtitle_text(data, segmented=resource.role == "subtitle")
            else:
                if resource.role == "muxed" and init:
                    data = await self.remux(data, init)
                    init = b""
                if resource.role:
                    from .preflight import inspect_media

                    started = time.monotonic()
                    evidence = await inspect_media(
                        init + data, self.config, resource.role, resource.duration
                    )
                    self.metrics["validation_seconds"] += time.monotonic() - started
                    self.evidence[key] = evidence
                    self.metrics["segments_verified"] = (
                        self.metrics.get("segments_verified", 0) + 1
                    )
                content_type = (
                    ("audio/mp4" if resource.role == "audio" else "video/mp4")
                    if init
                    else "video/mp2t"
                    if resource.role
                    else "application/octet-stream"
                )
                result = data, content_type
            self.put(key, result)
            return result

    async def _prefetch(self, key):
        try:
            async with self.prefetch_slots:
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
            task.add_done_callback(lambda done: self.forget(self.pending, key, done))
        return await asyncio.shield(self.pending[key])

    async def close(self):
        self.closed = True
        tasks = (
            list(self.tasks)
            + list(self.prefetches.values())
            + list(self.pending.values())
            + list(self.raw_pending.values())
        )
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.playlists.clear()
        self.selections.clear()
        self.audio_selections.clear()
        self.evidence.clear()
        self.checked_inits.clear()
        self.roles.clear()
        self.following.clear()
        self.selected_playlists.clear()
        self.cache.clear()
        self.cache_size = 0
