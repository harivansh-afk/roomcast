import unittest
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestServer

from roomcast.roku import Roku


class RokuTests(unittest.IsolatedAsyncioTestCase):
    async def test_serial_mismatch_blocks_commands(self):
        roku = Roku("10.0.0.2", "expected")
        roku.request = AsyncMock(
            return_value=b"<device-info><serial-number>other</serial-number></device-info>"
        )
        try:
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                await roku.command("home")
            roku.request.assert_awaited_once_with("/query/device-info")
        finally:
            await roku.close()

    async def test_launch_percent_encodes_nested_url_and_reads_chunked_xml(self):
        paths = []

        async def handler(request):
            paths.append(request.raw_path)
            if request.path == "/query/device-info":
                response = web.StreamResponse()
                await response.prepare(request)
                for fragment in [
                    b"<device-info><serial-number>ok</serial-number>",
                    b"<ecp-setting-mode>enabled</ecp-setting-mode></device-info>",
                ]:
                    await response.write(fragment)
                return response
            return web.Response()

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", handler)
        async with TestServer(app) as server:
            roku = Roku("10.0.0.2", "ok", "dev")
            roku.base = str(server.make_url(""))
            try:
                await roku.launch(
                    "http://10.0.0.1:18796/a?x=1&y=2", "Title", start_seconds=1200
                )
                self.assertTrue(paths[-1].startswith("/launch/dev?"))
                self.assertIn("startSeconds=1200", paths[-1])
                self.assertIn("u=http%3A%2F%2F", paths[-1])
                self.assertIn("%26y%3D2", paths[-1])
            finally:
                await roku.close()

    async def test_pause_is_idempotent(self):
        roku = Roku("10.0.0.2", "ok")
        roku.verify = AsyncMock()
        roku.status = AsyncMock(return_value={"app_id": roku.app_id, "state": "pause"})
        roku.request = AsyncMock()
        try:
            await roku.command("pause")
            roku.request.assert_not_awaited()
        finally:
            await roku.close()

    def test_public_command_contract_matches_roku_adapter(self):
        from typing import get_args

        from roomcast.client import Command

        self.assertEqual(set(get_args(Command)), set(Roku.commands))

    async def test_status_includes_real_device_audio_and_video_formats(self):
        roku = Roku("10.0.0.2", "ok")
        roku.verify = AsyncMock()
        roku.request = AsyncMock(
            side_effect=[
                b'<active-app><app id="782875">Media Assistant</app></active-app>',
                b'<player state="play" error="false"><plugin id="782875"/><format audio="none" video="mpeg4_10b" container="hls"/><position>1200 ms</position></player>',
            ]
        )
        try:
            state = await roku.status()
            self.assertEqual(state["audio_format"], "none")
            self.assertEqual(state["video_format"], "mpeg4_10b")
            self.assertFalse(roku.has_av(state))
        finally:
            await roku.close()

    async def test_progress_without_both_tracks_never_confirms(self):
        import asyncio
        from unittest.mock import patch

        real_sleep = asyncio.sleep

        async def tick(_):
            await real_sleep(0.001)

        for missing in ("audio_format", "video_format"):
            roku = Roku("10.0.0.2", "ok")
            position = 0

            async def status():
                nonlocal position
                position += 1000
                return {
                    "app_id": roku.app_id,
                    "player_app_id": roku.app_id,
                    "state": "play",
                    "error": False,
                    "position_ms": position,
                    "audio_format": "aac",
                    "video_format": "mpeg4_10b",
                    missing: "none",
                }

            roku.status = status
            try:
                with (
                    patch("roomcast.roku.asyncio.sleep", side_effect=tick),
                    self.assertRaisesRegex(ValueError, "audio and video"),
                ):
                    await roku.confirm(seconds=0.02)
            finally:
                await roku.close()

    async def test_confirmation_requires_current_session_delivery_and_sustained_av(
        self,
    ):
        from unittest.mock import patch

        roku = Roku("10.0.0.2", "ok")
        position = 0

        async def status():
            nonlocal position
            position += 1000
            return {
                "app_id": roku.app_id,
                "player_app_id": roku.app_id,
                "state": "play",
                "error": False,
                "position_ms": position,
                "audio_format": "aac",
                "video_format": "mpeg4_10b",
            }

        roku.status = status
        try:
            with patch("roomcast.roku.asyncio.sleep", new=AsyncMock()):
                result = await roku.confirm(delivered=lambda: position >= 5000)
            self.assertEqual(result["position_ms"], 6000)
        finally:
            await roku.close()
