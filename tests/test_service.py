import asyncio
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from roomcast.server import Service, errors
from roomcast.relay import Session
from test_relay import config, BASE


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Service(config())
        self.service.roku.status = AsyncMock(return_value={"state": "stop", "app_id": "782875"})
        self.service.roku.command = AsyncMock(return_value={"state": "stop"})
        app = web.Application(middlewares=[errors])
        app.add_routes([web.post("/play", self.service.start_play),
                        web.post("/command/{command}", self.service.command),
                        web.get("/media/{token}/{key}", self.service.media)])
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def test_unknown_media_token_is_not_an_open_proxy(self):
        response = await self.client.get("/media/invalid/https://evil.example")
        self.assertEqual(response.status, 404)

    async def test_replacement_requires_explicit_flag(self):
        self.service.roku.status.return_value = {"state": "play", "app_id": "12"}
        response = await self.client.post("/play", json={"kind": "tv", "id": 1})
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.service.job)

    async def test_stop_cancels_resolution_before_any_late_launch(self):
        gate = asyncio.Event()
        async def resolve(*args):
            gate.set()
            await asyncio.Event().wait()
        self.service.resolver.resolve = resolve
        self.service.roku.launch = AsyncMock()
        response = await self.client.post("/play", json={"kind": "tv", "id": 1})
        self.assertEqual(response.status, 202)
        await asyncio.wait_for(gate.wait(), 1)
        response = await self.client.post("/command/stop")
        self.assertEqual(response.status, 200)
        self.service.roku.launch.assert_not_awaited()
        self.assertEqual(self.service.job_state["state"], "stopped")

    async def test_invalid_command_is_rejected(self):
        response = await self.client.post("/command/PowerOff")
        self.assertEqual(response.status, 400)
        self.service.roku.command.assert_not_awaited()

    async def test_range_response(self):
        session = Session(AsyncMock(), config(), BASE + "main.m3u8", {}, "test")
        self.service.session = session
        session.put(session.root, (b"0123456789", "video/mp2t"))
        response = await self.client.get(f"/media/{session.token}/{session.root}", headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"2345")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
