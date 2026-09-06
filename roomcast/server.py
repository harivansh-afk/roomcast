import argparse
import asyncio
import logging
import os
import re
import time
from contextlib import aclosing
from pathlib import Path

from aiohttp import web

from .config import Config
from .fetch import Fetcher
from .relay import Session
from .resolver import Resolver
from .roku import Roku


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
        self.fetcher = Fetcher(config.allowed_hosts)
        self.roku = Roku(config.roku_ip, config.roku_serial)
        self.resolver = Resolver(config)
        self.session = None
        self.control_lock = asyncio.Lock()
        self.job = None
        self.job_state = {"state": "idle"}

    async def prepare(self, session):
        data, content_type = await session.get(session.root)
        if content_type != "application/vnd.apple.mpegurl":
            raise ValueError("source did not provide an HLS playlist")
        for line in data.decode().splitlines():
            if line and not line.startswith("#"):
                child = line.rsplit("/", 1)[-1]
                child_data, child_type = await session.get(child)
                if child_type == "application/vnd.apple.mpegurl":
                    for segment in child_data.decode().splitlines():
                        if segment and not segment.startswith("#"):
                            await session.get(segment.rsplit("/", 1)[-1])
                            break
                break

    async def play(self, body):
        started = time.monotonic()
        self.job_state = {"state": "resolving", "started_at": time.time()}
        candidate = None
        failures = []
        try:
            sources = self.resolver.resolve(
                body["kind"], body["id"], body.get("season", 1), body.get("episode", 1)
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
                            await self.prepare(candidate)
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
        }:
            raise ValueError("expected kind, id, season, episode, replace")
        if self.job and not self.job.done():
            raise web.HTTPConflict(text="a playback request is already running")
        if (
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
        current = await self.roku.status()
        if current["state"] == "play" and body.get("replace") is not True:
            raise web.HTTPConflict(text="TV is playing; explicit replace=true required")
        self.job_state = {"state": "queued"}
        self.job = asyncio.create_task(self.play(body))
        return web.json_response(self.job_state, status=202)

    async def status(self, request):
        result = {"job": self.job_state, "roku": await self.roku.status()}
        if self.session:
            result["relay"] = {
                **self.session.metrics,
                "cache_bytes": self.session.cache_size,
            }
        return web.json_response(result)

    async def search(self, request):
        return web.json_response(await self.resolver.search(request.query.get("q", "")))

    async def command(self, request):
        async with self.control_lock:
            return await self._command(request)

    async def _command(self, request):
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
            self.job_state = {"state": "stopped"}
        elif self.job and not self.job.done():
            raise web.HTTPConflict(text="wait for playback or stop the pending request")
        return web.json_response(await self.roku.command(command))

    async def media(self, request):
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
        await self.resolver.close()
        await self.roku.close()
        await self.fetcher.close()


async def serve(config):
    service = Service(config)
    control = web.Application(middlewares=[errors], client_max_size=4096)
    control.add_routes(
        [
            web.get("/status", service.status),
            web.get("/search", service.search),
            web.post("/play", service.start_play),
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
        await web.TCPSite(runners[1], config.media_host, config.media_port).start()
        logging.info(
            "roomcast ready; control=%s media=loopback:%s", socket, config.media_port
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
