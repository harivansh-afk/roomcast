import asyncio
import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_relay import BASE, config

from roomcast.relay import Session
from roomcast.server import Service, errors


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Service(config())
        self.service.roku.status = AsyncMock(
            return_value={"state": "stop", "app_id": "782875"}
        )
        self.service.roku.command = AsyncMock(return_value={"state": "stop"})
        app = web.Application(middlewares=[errors])
        app.add_routes(
            [
                web.post("/play", self.service.start_play),
                web.post("/command/{command}", self.service.command),
                web.get("/media/{token}/{key}", self.service.media),
            ]
        )
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
            yield  # This resolver never produces a source before cancellation.

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
        response = await self.client.get(
            f"/media/{session.token}/{session.root}", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(response.status, 206)
        self.assertEqual(await response.read(), b"2345")
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")

    async def test_concurrent_play_has_one_winner(self):
        async def delayed_status():
            await asyncio.sleep(0.02)
            return {"state": "stop", "app_id": "782875"}

        self.service.roku.status = delayed_status

        async def resolving(*args):
            await asyncio.Event().wait()
            yield  # This resolver never produces a source before cancellation.

        self.service.resolver.resolve = resolving
        responses = await asyncio.gather(
            *[self.client.post("/play", json={"kind": "tv", "id": 1}) for _ in range(2)]
        )
        self.assertEqual(sorted(r.status for r in responses), [202, 409])

    async def test_invalid_play_is_rejected_before_roku_or_browser_io(self):
        self.service.resolver.resolve = AsyncMock()
        for body in (
            {"kind": "tv", "id": True},
            {"kind": "other", "id": 1},
            {"kind": "tv", "id": 1, "season": 0},
            {"kind": "tv", "id": 1, "episode": "1"},
            {"kind": "tv", "id": 1, "replace": "false"},
        ):
            with self.subTest(body=body):
                response = await self.client.post("/play", json=body)
                self.assertEqual(response.status, 400)
        self.service.roku.status.assert_not_awaited()
        self.service.resolver.resolve.assert_not_awaited()
        self.assertIsNone(self.service.job)

    async def test_replacement_has_no_app_specific_bypass(self):
        self.service.roku.status.return_value = {"state": "play", "app_id": "562859"}
        response = await self.client.post("/play", json={"kind": "tv", "id": 1})
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.service.job)

    async def test_failed_provider_advances_and_success_closes_resolver(self):
        visited = []
        closed = asyncio.Event()

        async def sources(*args):
            try:
                for provider in ("Unavailable", "Replacement", "Unused"):
                    visited.append(provider)
                    yield {
                        "title": "test",
                        "provider": provider,
                        "sources": [BASE + provider + ".m3u8"],
                        "headers": {},
                    }
            finally:
                closed.set()

        self.service.resolver.resolve = sources
        self.service.prepare = AsyncMock(
            side_effect=[ValueError("upstream HTTP 404"), None]
        )
        self.service.roku.launch = AsyncMock()
        self.service.roku.confirm = AsyncMock()
        await self.service.play({"kind": "tv", "id": 1})
        self.assertEqual(visited, ["Unavailable", "Replacement"])
        self.assertTrue(closed.is_set())
        self.assertEqual(self.service.job_state["state"], "playing")
        self.assertEqual(self.service.job_state["provider"], "Replacement")
        self.service.roku.launch.assert_awaited_once()

    async def test_all_failed_providers_report_failure_without_launch(self):
        async def sources(*args):
            for provider in ("One", "Two"):
                yield {
                    "title": "test",
                    "provider": provider,
                    "sources": [BASE + provider + ".m3u8"],
                    "headers": {},
                }

        self.service.resolver.resolve = sources
        self.service.prepare = AsyncMock(side_effect=ValueError("upstream HTTP 404"))
        self.service.roku.launch = AsyncMock()
        await self.service.play({"kind": "tv", "id": 1})
        self.assertEqual(self.service.job_state["state"], "failed")
        self.assertIsNone(self.service.session)
        self.service.roku.launch.assert_not_awaited()
