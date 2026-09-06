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
        self.roku = Roku(config.roku_ip, config.roku_serial)
        self.network = Network(config, self.roku) if config.lan_interface else None
        self.resolver = Resolver(config)
        self.browser = Browser(self.resolver)
        self.session = None
        self.control_lock = asyncio.Lock()
        self.job = None
        self.job_state = {"state": "idle"}

    async def prepare(self, session):
        return await session.prepare()

    async def play(self, body):
        started = time.monotonic()
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
                await self.roku.confirm(app_id="837")
                self.job_state.update(
                    state="playing",
                    provider="YouTube",
                    startup_seconds=round(time.monotonic() - started, 3),
                )
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
                            previous, self.session = self.session, candidate
                            if previous:
                                await previous.close()
                            self.job_state["state"] = "launching"
                            await self.roku.launch(
                                candidate.link(candidate.root), candidate.title
                            )
                            self.job_state["state"] = "verifying"
                            await self.roku.confirm()
                            self.job_state.update(
                                state="playing",
                                startup_seconds=round(time.monotonic() - started, 3),
                            )
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
                            await candidate.close()
                            if self.session is candidate:
                                self.session = None
                            candidate = None
                    self.job_state["state"] = "resolving"
            raise ValueError(
                "sources failed: " + "; ".join(failures)
                if failures
                else "no supported source found; site may have changed"
            )
        except asyncio.CancelledError:
            if candidate and candidate is not self.session:
                await candidate.close()
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
        }:
            raise ValueError("expected kind, id, season, episode, replace")
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
        if self.network:
            await self.network.ensure()
            self.config.public_base = (
                f"http://{self.network.local_address}:{self.config.lan_port}"
            )
        current = await self.roku.status()
        if current["state"] == "play" and body.get("replace") is not True:
            raise web.HTTPConflict(text="TV is playing; explicit replace=true required")
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

    async def seek(self, request):
        async with self.control_lock:
            if self.job and not self.job.done():
                raise web.HTTPConflict(text="wait for playback before seeking")
            if self.network:
                await self.network.ensure()
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"seconds"}:
                raise ValueError("expected seconds")
            seconds = body["seconds"]
            if type(seconds) is not int or not 1 <= abs(seconds) <= 3600:
                raise ValueError("seconds must be a nonzero integer within one hour")
            before = await self.roku.status()
            if before["app_id"] != "837":
                raise ValueError("Precise seeking currently requires YouTube")
            target = max(0, before["position_ms"] / 1000 + seconds)
            await self.youtube.seek(target)
            for _ in range(5):
                await asyncio.sleep(1)
                after = await self.roku.status()
                if after["app_id"] != "837":
                    raise ValueError("TV app changed during seek")
                if abs(after["position_ms"] / 1000 - target) < 6:
                    return web.json_response(
                        {"confirmed": True, "target_seconds": target, "roku": after}
                    )
            raise ValueError(
                "YouTube seek was sent but the TV did not confirm the target position"
            )

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
        data, content_type = await session.get(key)
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
            return web.Response(
                body=data[start : end + 1],
                status=206,
                content_type=content_type,
                headers=headers,
            )
        return web.Response(body=data, content_type=content_type, headers=headers)

    async def close(self):
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
