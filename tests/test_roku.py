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
            roku = Roku("10.0.0.2", "ok")
            roku.base = str(server.make_url(""))
            try:
                await roku.launch("http://10.0.0.1:18796/a?x=1&y=2", "Title")
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
