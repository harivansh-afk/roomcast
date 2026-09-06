import argparse
import asyncio
import logging
import os
import re
import socket as sockets
import time
from contextlib import aclosing
from pathlib import Path

from aiohttp import web

from .browser import Browser
from .config import Config
from .directory import Directory
from .fetch import Fetcher
from .network import Network
from .relay import Session
from .resolver import Resolver
from .roku import Roku
from .youtube import YouTube


class InvalidPosition(ValueError):
    pass


def check_position(target, duration):
    if not duration or not 0 <= target < duration:
        raise InvalidPosition("timestamp must be within a video with a known duration")


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (ValueError, KeyError) as error:
        return web.json_response({"error": str(error)}, status=400)
    except Exception as error:
        logging.error("request failed (%s)", type(error).__name__)
        return web.json_response(
            {"error": "request failed; see service log"}, status=502
        )


class Service:
    def __init__(self, config):
        self.config = config
        self.youtube = YouTube(config.youtube_auth)
        self.directory = Directory(config.directory_url, config.directory_cache)
        self.fetcher = Fetcher(config.allowed_hosts)
        self.roku = Roku(config.roku_ip, config.roku_serial, config.roku_app_id)
        self.network = Network(config, self.roku) if config.lan_interface else None
        self.resolver = Resolver(config)
        self.browser = Browser(self.resolver)
        self.session = None
        self.control_lock = asyncio.Lock()
        self.job = None
        self.monitor = None
        self.job_state = {"state": "idle"}

    async def stop_monitor(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None

    def watch(self, app_id):
        self.monitor = asyncio.create_task(self.watch_playback(app_id))

    async def watch_playback(self, app_id):
        last_position = None
        last_progress = time.monotonic()
        unhealthy_since = None
        while True:
            await asyncio.sleep(3)
            try:
                state = await self.roku.status()
                now = time.monotonic()
                if state["app_id"] != app_id:
                    self.job_state["state"] = "ended"
                    break
                if self.session and self.session.failure:
                    raise ValueError(self.session.failure)
                if state.get("error"):
                    raise ValueError("Roku reported a playback error")
                if state["state"] == "pause":
                    self.job_state["state"] = "paused"
                    last_progress, unhealthy_since = now, None
                    continue
                if state["state"] in ("stop", "close", "finished"):
                    duration = state.get("duration_ms", 0)
                    if duration and state.get("position_ms", 0) >= duration - 3000:
                        self.job_state["state"] = "ended"
                        break
                    raise ValueError("Roku stopped before the end of the video")
                if state.get("player_app_id") != app_id or not self.roku.has_av(state):
                    unhealthy_since = unhealthy_since or now
                    self.job_state["state"] = "buffering"
                    if now - unhealthy_since >= 9:
                        raise ValueError("Roku lost the audio or video track")
                else:
                    unhealthy_since = None
                    self.job_state["state"] = (
                        "playing" if state["state"] == "play" else "buffering"
                    )
                position = state.get("position_ms", 0)
                if last_position is None or position != last_position:
                    last_progress = now
                elif now - last_progress >= 30:
                    raise ValueError("Roku playback stalled for 30 seconds")
                last_position = position
                self.job_state["checked_at"] = time.time()
            except asyncio.CancelledError:
                raise
            except ValueError as error:
                self.job_state.update(state="failed", error=str(error))
                logging.warning("playback failed: %s", error)
                break
            except Exception as error:
                self.job_state["state"] = "buffering"
                if time.monotonic() - last_progress >= 30:
                    self.job_state.update(
                        state="failed",
                        error=f"TV status unavailable ({type(error).__name__})",
                    )
                    break
        if self.session:
            await self.session.close()
            self.session = None

    async def prepare(self, session):
        return await session.prepare()

    async def play(self, body):
        try:
            async with asyncio.timeout(110):
                await self._play(body)
        except TimeoutError:
            self.job_state.update(
                state="failed", error="playback startup exceeded 110 seconds"
            )

    async def _play(self, body):
        await self.stop_monitor()
        started = time.monotonic()
        start = body.get("start_seconds", 0)
        self.job_state = {"state": "resolving", "started_at": time.time()}
        candidate = None
        failures = []
        try:
            if body["kind"] == "youtube":
                if self.session:
                    await self.session.close()
                    self.session = None
                self.job_state["state"] = "launching"
                await self.roku.launch_youtube(body["id"])
                self.job_state["state"] = "verifying"
                before = await self.roku.confirm(app_id="837")
                if start:
                    check_position(start, before.get("duration_ms", 0) / 1000)
                    self.job_state["state"] = "seeking"
                    seek_started = time.monotonic()
                    await self.youtube.seek(start)
                    result = await self.roku.confirm_seek(
                        start, before, started=seek_started
                    )
                    self.job_state["seek"] = result
                self.job_state.update(
                    state="playing",
                    provider="YouTube",
                    startup_seconds=round(time.monotonic() - started, 3),
                )
                self.watch("837")
                return
            if body["kind"] == "browser":
                sources = self.browser.selected(body["id"])
            else:
                origin = await self.site(body.get("source", "cinejoy"))
                sources = self.resolver.resolve(
                    body["kind"],
                    body["id"],
                    body.get("season", 1),
                    body.get("episode", 1),
                    origin=origin,
                )
            async with aclosing(sources):
                async for resolved in sources:
                    self.job_state.update(
                        title=resolved["title"], provider=resolved["provider"]
                    )
                    for source in resolved["sources"]:
                        self.job_state["state"] = "preparing"
                        candidate = Session(
                            self.fetcher,
                            self.config,
                            source,
                            resolved["headers"],
                            resolved["title"],
                        )
                        try:
                            try:
                                await self.prepare(candidate)
                            finally:
                                self.job_state["preflight"] = candidate.preflight
                            if start:
                                check_position(
                                    start,
                                    candidate.preflight.get("duration_seconds", 0),
                                )
                            previous, self.session = self.session, candidate
                            if previous:
                                await previous.close()
                            self.job_state["state"] = "launching"
                            await self.roku.launch(
                                candidate.link(candidate.root),
                                candidate.title,
                                **({"start_seconds": start} if start else {}),
                            )
                            self.job_state["state"] = "verifying"

                            def delivered():
                                if candidate.failure:
                                    raise ValueError(candidate.failure)
                                return {"audio", "video"} <= candidate.delivered

                            state = await self.roku.confirm(
                                delivered=delivered,
                                **({"start_seconds": start} if start else {}),
                            )
                            if start:
                                self.job_state.update(
                                    start_seconds=start,
                                    actual_seconds=state["position_ms"] / 1000,
                                )
                            self.job_state.update(
                                state="playing",
                                startup_seconds=round(time.monotonic() - started, 3),
                            )
                            self.watch(self.roku.app_id)
                            return
                        except Exception as error:
                            failures.append(
                                f"{resolved['provider']}: "
                                + (
                                    str(error)
                                    if isinstance(error, ValueError)
                                    else type(error).__name__
                                )
                            )
                            logging.warning("candidate failed: %s", failures[-1])
                            await candidate.close()
                            if self.session is candidate:
                                self.session = None
                            candidate = None
                            if isinstance(error, InvalidPosition):
                                raise
                    self.job_state["state"] = "resolving"
            raise ValueError(
                "sources failed: " + "; ".join(failures)
                if failures
                else "no supported source found; site may have changed"
            )
        except asyncio.CancelledError:
            if candidate:
                await candidate.close()
                if self.session is candidate:
                    self.session = None
            self.job_state["state"] = "cancelled"
            raise
        except Exception as error:
            self.job_state.update(
                state="failed",
                error=str(error)[:300]
                if isinstance(error, ValueError)
                else f"playback failed ({type(error).__name__})",
            )

    async def start_play(self, request):
        async with self.control_lock:
            return await self._start_play(request)

    async def _start_play(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {
            "kind",
            "id",
            "season",
            "episode",
            "replace",
            "source",
            "start_seconds",
        }:
            raise ValueError(
                "expected kind, id, season, episode, replace, source, start_seconds"
            )
        if self.job and not self.job.done():
            raise web.HTTPConflict(text="a playback request is already running")
        if body.get("kind") == "browser":
            if not isinstance(body.get("id"), str) or not re.fullmatch(
                r"[a-f0-9]{16}", body["id"]
            ):
                raise ValueError("invalid captured stream ID")
        elif body.get("kind") == "youtube":
            if not isinstance(body.get("id"), str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{11}", body["id"]
            ):
                raise ValueError("invalid YouTube video ID")
        elif (
            body.get("kind") not in ("tv", "movie")
            or type(body.get("id")) is not int
            or not 0 < body["id"] < 100000000
        ):
            raise ValueError("invalid content identifier")
        if any(
            type(body.get(key, 1)) is not int or not 1 <= body.get(key, 1) <= 1000
            for key in ("season", "episode")
        ):
            raise ValueError("invalid season or episode")
        if type(body.get("replace", False)) is not bool:
            raise ValueError("replace must be a boolean")
        start = body.get("start_seconds", 0)
        if type(start) is not int or not 0 <= start <= 21600:
            raise ValueError("start_seconds must be an integer from 0 to 21600")
        if start:
            if body["kind"] == "youtube":
                if not self.youtube.path.exists():
                    raise ValueError("Pair YouTube before requesting a start timestamp")
            else:
                self.require_seeking_player()
        if self.network:
            await self.network.ensure()
            self.config.public_base = (
                f"http://{self.network.local_address}:{self.config.lan_port}"
            )
        current = await self.roku.status()
        if (
            current["state"] in ("play", "pause", "buffer")
            and body.get("replace") is not True
        ):
            raise web.HTTPConflict(
                text="TV has active playback; explicit replace=true required"
            )
        self.job_state = {"state": "queued"}
        self.job = asyncio.create_task(self.play(body))
        return web.json_response(self.job_state, status=202)

    async def status(self, request):
        if self.network:
            await self.network.ensure()
        result = {"job": self.job_state, "roku": await self.roku.status()}
        if self.session:
            result["preflight"] = self.session.preflight
            result["relay"] = {
                **self.session.metrics,
                "cache_bytes": self.session.cache_size,
            }
        return web.json_response(result)

    async def browse(self, request):
        async with self.control_lock:
            if self.job and not self.job.done():
                raise web.HTTPConflict(
                    text="wait for playback or stop the pending request"
                )
            return await self._browse(request)

    async def _browse(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {
            "source",
            "action",
            "element",
            "text",
        }:
            raise ValueError("expected source, action, element and text")
        origin = await self.site(body.get("source", "cinejoy"))
        return web.json_response(
            await self.browser.act(
                origin,
                body.get("action", "inspect"),
                body.get("element"),
                body.get("text"),
            )
        )

    async def sources(self, request):
        return web.json_response(await self.directory.list())

    async def site(self, source):
        if source == "cinejoy":
            return self.config.site_url
        for item in await self.directory.list():
            if source == item["id"]:
                return item["url"].rstrip("/")
        raise ValueError("unknown source; choose an ID returned by sources")

    async def search(self, request):
        source = request.query.get("source", "cinejoy")
        query = request.query.get("q", "")
        if source == "youtube":
            return web.json_response(await self.resolver.search_youtube(query))
        origin = await self.site(source)
        results = await self.resolver.search(query, origin=origin)
        return web.json_response([{**item, "source": source} for item in results])

    def require_seeking_player(self):
        if not self.config.roku_seek_enabled:
            raise ValueError(
                "Timestamp commands require the optional Roomcast Roku player; "
                "install it and configure roku_app_id and roku_seek_enabled first"
            )

    async def seek(self, request):
        async with self.control_lock:
            if self.job and not self.job.done():
                raise web.HTTPConflict(text="wait for playback before seeking")
            body = await request.json()
            if not isinstance(body, dict) or set(body) - {"seconds", "mode"}:
                raise ValueError("expected seconds and optional mode")
            seconds, mode = body.get("seconds"), body.get("mode", "relative")
            if mode not in ("relative", "absolute"):
                raise ValueError("mode must be relative or absolute")
            if type(seconds) is not int or not (
                0 <= seconds <= 21600
                if mode == "absolute"
                else 1 <= abs(seconds) <= 3600
            ):
                raise ValueError(
                    "absolute seconds must be 0..21600; relative seconds must be nonzero and within one hour"
                )
            if self.network:
                await self.network.ensure()
            before = await self.roku.status()
            if before["app_id"] != "837":
                self.require_seeking_player()
                if before["app_id"] != self.roku.app_id:
                    raise ValueError("The configured Roomcast player is not active")
            if (
                before["state"] not in ("play", "pause")
                or before.get("player_app_id") != before["app_id"]
            ):
                raise ValueError(
                    "Wait for the video to be playing or paused before seeking"
                )
            target = (
                seconds
                if mode == "absolute"
                else max(0, before["position_ms"] // 1000 + seconds)
            )
            check_position(target, before.get("duration_ms", 0) / 1000)
            await self.stop_monitor()
            self.job_state.update(state="seeking", target_seconds=target)
            self.job = job = asyncio.create_task(self.perform_seek(target, before))
        try:
            return web.json_response(await asyncio.shield(job))
        except asyncio.CancelledError:
            if job.cancelled():
                raise web.HTTPConflict(text="seek cancelled by stop or home") from None
            raise

    async def perform_seek(self, target, before):
        try:
            async with asyncio.timeout(65):
                if before["app_id"] == "837":
                    started = time.monotonic()
                    await self.youtube.seek(target)
                    result = await self.roku.confirm_seek(
                        target, before, started=started
                    )
                else:
                    result = await self.roku.seek_to(target, before)
            self.job_state.pop("seek_error", None)
            self.job_state.update(
                state="paused" if before["state"] == "pause" else "playing",
                seek=result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.job_state.update(state="seek_failed", seek_error=str(error)[:300])
            self.watch(before["app_id"])
            raise
        else:
            self.watch(before["app_id"])
            return result

    async def pair_youtube(self, request):
        async with self.control_lock:
            body = await request.json()
            return web.json_response(await self.youtube.pair(body.get("code")))

    async def command(self, request):
        async with self.control_lock:
            return await self._command(request)

    async def _command(self, request):
        if self.network:
            await self.network.ensure()
        command = request.match_info["command"]
        if command not in self.roku.commands:
            raise ValueError("unsupported command")
        if command in ("stop", "home"):
            await self.stop_monitor()
            if self.job and not self.job.done():
                self.job.cancel()
                await asyncio.gather(self.job, return_exceptions=True)
            if self.session:
                await self.session.close()
                self.session = None
            await self.browser.close()
            self.job_state = {"state": "stopped"}
        elif self.job and not self.job.done():
            raise web.HTTPConflict(text="wait for playback or stop the pending request")
        return web.json_response(await self.roku.command(command))

    async def media(self, request):
        if self.network and request.remote != self.network.address:
            raise web.HTTPNotFound()
        session = self.session
        if (
            session is None
            or request.match_info["token"] != session.token
            or session.closed
        ):
            raise web.HTTPNotFound()
        key = request.match_info["key"]
        if key not in session.resources:
            raise web.HTTPNotFound()
        try:
            data, content_type = await session.get(key)
        except Exception as error:
            reason = (
                str(error) if isinstance(error, ValueError) else type(error).__name__
            )
            session.failure = "media delivery failed: " + reason[:200]
            logging.warning("%s", session.failure)
            raise web.HTTPBadGateway(text=session.failure) from None
        # Roku normally requests whole HLS segments; implement a single byte range for clients that do not.
        headers = {"Cache-Control": "private, max-age=60", "Accept-Ranges": "bytes"}
        if value := request.headers.get("Range"):
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", value)
            if not match:
                raise web.HTTPRequestRangeNotSatisfiable()
            start = int(match[1])
            end = min(int(match[2]) if match[2] else len(data) - 1, len(data) - 1)
            if start > end or start >= len(data):
                raise web.HTTPRequestRangeNotSatisfiable()
            headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
            if request.method != "HEAD":
                self.record_delivery(session, key)
            return web.Response(
                body=data[start : end + 1],
                status=206,
                content_type=content_type,
                headers=headers,
            )
        if request.method != "HEAD":
            self.record_delivery(session, key)
        return web.Response(body=data, content_type=content_type, headers=headers)

    @staticmethod
    def record_delivery(session, key):
        role = session.resources[key].role
        session.delivered.update(
            ("video", "audio") if role == "muxed" else (role,) if role else ()
        )

    async def close(self):
        await self.stop_monitor()
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        if self.session:
            await self.session.close()
        await self.browser.close()
        await self.resolver.close()
        await self.roku.close()
        await self.fetcher.close()
        await self.directory.close()
        await self.youtube.close()


async def serve(config):
    service = Service(config)
    control = web.Application(middlewares=[errors], client_max_size=4096)
    control.add_routes(
        [
            web.get("/status", service.status),
            web.get("/search", service.search),
            web.get("/sources", service.sources),
            web.post("/browse", service.browse),
            web.post("/play", service.start_play),
            web.post("/seek", service.seek),
            web.post("/youtube/pair", service.pair_youtube),
            web.post("/command/{command}", service.command),
        ]
    )
    media = web.Application(middlewares=[errors], client_max_size=1024)
    media.add_routes([web.get("/media/{token}/{key}", service.media)])
    runners = [
        web.AppRunner(control, access_log=None),
        web.AppRunner(media, access_log=None),
    ]
    socket = Path(config.control_socket)
    socket.parent.mkdir(parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    try:
        for runner in runners:
            await runner.setup()
        await web.UnixSite(runners[0], str(socket)).start()
        os.chmod(socket, 0o660)
        if config.lan_interface:
            if (
                os.environ.get("LISTEN_PID") != str(os.getpid())
                or os.environ.get("LISTEN_FDS") != "1"
            ):
                raise ValueError("LAN mode requires the systemd media socket")
            listener = sockets.socket(fileno=3)
            await web.SockSite(runners[1], listener).start()
        else:
            await web.TCPSite(runners[1], config.media_host, config.media_port).start()
        logging.info(
            "roomcast ready; control=%s media=%s",
            socket,
            "LAN socket" if config.lan_interface else f"loopback:{config.media_port}",
        )
        await asyncio.Event().wait()
    finally:
        for runner in runners:
            await runner.cleanup()
        await service.close()
        socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(serve(Config.load(args.config)))
    except KeyboardInterrupt:
        pass
