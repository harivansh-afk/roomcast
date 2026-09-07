import argparse
import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_relay import BASE, config

from roomcast.cli import main, timestamp
from roomcast.roku import Roku
from roomcast.server import Service, errors


def playback(position=100, state="play", app="dev", **extra):
    return {
        "app_id": app,
        "player_app_id": app,
        "state": state,
        "error": False,
        "position_ms": int(position * 1000),
        "duration_ms": 1800000,
        "audio_format": "aac",
        "video_format": "mpeg4_10b",
        **extra,
    }


class SeekingAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        settings = config()
        settings.roku_app_id, settings.roku_seek_enabled = "dev", True
        self.service = Service(settings)
        self.service.roku.request = AsyncMock(
            side_effect=AssertionError("unexpected device IO")
        )
        self.service.roku.status = AsyncMock(return_value=playback())
        self.service.roku.launch = AsyncMock()
        self.service.roku.confirm = AsyncMock(return_value=playback(1201))
        self.service.roku.command = AsyncMock(return_value={"state": "stop"})
        self.service.roku.seek_to = AsyncMock(return_value={"confirmed": True})
        self.service.youtube.seek = AsyncMock()
        app = web.Application(middlewares=[errors])
        app.add_routes(
            [
                web.post("/play", self.service.start_play),
                web.post("/seek", self.service.seek),
                web.post("/command/{command}", self.service.command),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def test_absolute_relative_and_paused_seek(self):
        for body, target, state in [
            ({"seconds": 1200, "mode": "absolute"}, 1200, "play"),
            ({"seconds": 0, "mode": "absolute"}, 0, "play"),
            ({"seconds": 30}, 130, "play"),
            ({"seconds": -300}, 0, "play"),
            ({"seconds": -30}, 70, "pause"),
        ]:
            with self.subTest(body=body):
                before = playback(state=state)
                self.service.roku.status.return_value = before
                response = await self.client.post("/seek", json=body)
                self.assertEqual(response.status, 200, await response.text())
                self.service.roku.seek_to.assert_awaited_with(target, before)
                self.assertEqual(
                    self.service.job_state["state"],
                    "paused" if state == "pause" else "playing",
                )
                self.assertIsNotNone(self.service.monitor)

    async def test_invalid_inputs_do_not_query_or_control_tv(self):
        for body in [
            [],
            {},
            {"seconds": True},
            {"seconds": 0},
            {"seconds": 3601},
            {"seconds": -1, "mode": "absolute"},
            {"seconds": 1.5},
            {"seconds": 21601, "mode": "absolute"},
            {"seconds": 1, "mode": "bad"},
            {"seconds": 1, "url": "bad"},
        ]:
            response = await self.client.post("/seek", json=body)
            self.assertEqual(response.status, 400, body)
        self.service.roku.status.assert_not_awaited()
        self.service.roku.seek_to.assert_not_awaited()

    async def test_unsupported_and_out_of_range_never_send_seek(self):
        for before, enabled in [
            (playback(app="782875"), False),
            (playback(app="other"), True),
            (playback(state="buffer"), True),
            (playback(player_app_id="old"), True),
            (playback(duration_ms=0), True),
            (playback(duration_ms=1200000), True),
        ]:
            self.service.config.roku_seek_enabled = enabled
            self.service.roku.status.return_value = before
            response = await self.client.post(
                "/seek", json={"seconds": 1200, "mode": "absolute"}
            )
            self.assertEqual(response.status, 400, await response.text())
        self.service.roku.seek_to.assert_not_awaited()
        self.service.youtube.seek.assert_not_awaited()

    async def test_youtube_absolute_seek_uses_paired_adapter(self):
        self.service.roku.status.return_value = playback(app="837")
        self.service.roku.confirm_seek = AsyncMock(return_value={"confirmed": True})
        response = await self.client.post(
            "/seek", json={"seconds": 1200, "mode": "absolute"}
        )
        self.assertEqual(response.status, 200)
        self.service.youtube.seek.assert_awaited_once_with(1200)
        self.service.roku.confirm_seek.assert_awaited_once()
        self.service.roku.seek_to.assert_not_awaited()

    async def test_failed_seek_resumes_monitor_and_reports_failure(self):
        self.service.roku.seek_to.side_effect = ValueError("seek not confirmed")
        response = await self.client.post(
            "/seek", json={"seconds": 1200, "mode": "absolute"}
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.service.job_state["seek_error"], "seek not confirmed")
        self.assertIsNotNone(self.service.monitor)

    async def test_stop_cancels_seek_without_late_state_update(self):
        entered = asyncio.Event()

        async def seeking(*args):
            entered.set()
            await asyncio.Event().wait()

        self.service.roku.seek_to = seeking
        seeking_request = asyncio.create_task(
            self.client.post("/seek", json={"seconds": 1200, "mode": "absolute"})
        )
        await asyncio.wait_for(entered.wait(), 1)
        conflict = await self.client.post("/seek", json={"seconds": 30})
        self.assertEqual(conflict.status, 409)
        response = await asyncio.wait_for(self.client.post("/command/stop"), 1)
        self.assertEqual(response.status, 200)
        response = await asyncio.wait_for(seeking_request, 1)
        self.assertEqual(response.status, 409)
        self.assertEqual(self.service.job_state, {"state": "stopped"})
        self.assertIsNone(self.service.monitor)

    async def test_invalid_or_unsupported_start_fails_before_device_io(self):
        for start in [True, -1, 1.5, "20:00", 21601]:
            response = await self.client.post(
                "/play", json={"kind": "tv", "id": 1, "start_seconds": start}
            )
            self.assertEqual(response.status, 400)
        self.service.config.roku_seek_enabled = False
        response = await self.client.post(
            "/play", json={"kind": "tv", "id": 1, "start_seconds": 1200}
        )
        self.assertEqual(response.status, 400)
        self.assertIn("optional Roomcast Roku player", await response.text())
        self.service.roku.status.assert_not_awaited()
        self.service.roku.launch.assert_not_awaited()

    async def test_paused_playback_requires_explicit_replacement(self):
        self.service.roku.status.return_value = playback(state="pause")
        response = await self.client.post(
            "/play", json={"kind": "tv", "id": 1, "start_seconds": 1200}
        )
        self.assertEqual(response.status, 409)
        self.service.roku.launch.assert_not_awaited()

    async def test_youtube_start_requires_pairing_before_device_io(self):
        self.service.youtube.path = SimpleNamespace(exists=lambda: False)
        response = await self.client.post(
            "/play",
            json={
                "kind": "youtube",
                "id": "aqz-KE-bpKQ",
                "start_seconds": 1200,
            },
        )
        self.assertEqual(response.status, 400)
        self.assertIn("Pair YouTube", await response.text())
        self.service.roku.status.assert_not_awaited()
        self.service.youtube.seek.assert_not_awaited()

    async def test_youtube_start_seeks_after_native_launch_confirmation(self):
        self.service.youtube.path = SimpleNamespace(exists=lambda: True)
        self.service.roku.launch_youtube = AsyncMock()
        self.service.roku.confirm.return_value = playback(1, app="837")
        self.service.roku.confirm_seek = AsyncMock(return_value={"confirmed": True})
        response = await self.client.post(
            "/play",
            json={
                "kind": "youtube",
                "id": "aqz-KE-bpKQ",
                "start_seconds": 1200,
                "replace": True,
            },
        )
        self.assertEqual(response.status, 202)
        await self.service.job
        self.service.roku.launch_youtube.assert_awaited_once_with("aqz-KE-bpKQ")
        self.service.roku.confirm.assert_awaited_once_with(app_id="837")
        self.service.youtube.seek.assert_awaited_once_with(1200)
        self.assertTrue(self.service.job_state["seek"]["confirmed"])
        self.assertEqual(self.service.job_state["state"], "playing")

    async def test_play_start_passes_absolute_timestamp_to_player(self):
        async def sources(*args, **kwargs):
            yield {
                "title": "Episode",
                "provider": "fixture",
                "sources": [BASE + "video.m3u8"],
                "headers": {},
            }

        async def prepare(session):
            session.preflight["duration_seconds"] = 1800

        self.service.resolver.resolve = sources
        self.service.prepare = prepare
        response = await self.client.post(
            "/play",
            json={"kind": "tv", "id": 1, "replace": True, "start_seconds": 1200},
        )
        self.assertEqual(response.status, 202)
        await self.service.job
        self.service.roku.launch.assert_awaited_once_with(
            self.service.session.link(self.service.session.root),
            "Episode",
            start_seconds=1200,
            report_url=f"{self.service.config.public_base}/player-state/{self.service.session.token}",
        )
        self.assertEqual(
            self.service.roku.confirm.call_args.kwargs["start_seconds"], 1200
        )
        self.assertEqual(self.service.job_state["actual_seconds"], 1201)
        self.assertEqual(self.service.job_state["state"], "playing")
        await self.service.stop_monitor()
        await self.service.play({"kind": "tv", "id": 1, "start_seconds": 1800})
        self.assertEqual(self.service.job_state["state"], "failed")
        self.assertIn("within a video", self.service.job_state["error"])
        self.assertEqual(self.service.roku.launch.await_count, 1)


class RokuSeekingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.roku = Roku("10.0.0.2", "ok", "dev")
        self.roku.request = AsyncMock()
        self.roku.verify = AsyncMock()

    async def asyncTearDown(self):
        await self.roku.close()

    async def test_launch_and_seek_protocol(self):
        await self.roku.launch(
            "http://10.0.0.1/video.m3u8", "Episode", start_seconds=1200
        )
        self.assertEqual(self.roku.request.call_args.args[0], "/launch/dev")
        self.assertEqual(self.roku.request.call_args.args[1]["startSeconds"], 1200)
        self.roku.confirm_seek = AsyncMock(return_value={"confirmed": True})
        await self.roku.seek_to(1200, playback(state="pause"))
        self.roku.request.assert_awaited_with(
            "/input", {"a": "seek", "positionSeconds": 1200, "stayPaused": "true"}
        )

    async def test_confirm_start_ignores_buffer_position_but_rejects_wrong_start(self):
        for position, success in [(1200, True), (0, False), (1600, False)]:
            self.roku.status = AsyncMock(
                side_effect=[
                    playback(0, state="buffer"),
                    playback(position),
                    playback(position + 1),
                    playback(position + 2),
                ]
            )
            with patch("roomcast.roku.asyncio.sleep", new=AsyncMock()):
                if success:
                    await self.roku.confirm(start_seconds=1200)
                else:
                    with self.assertRaisesRegex(ValueError, "requested timestamp"):
                        await self.roku.confirm(start_seconds=1200)

    async def test_confirm_seek_waits_for_landing_and_checks_pause_and_av(self):
        for state in ["play", "pause"]:
            self.roku.status = AsyncMock(
                side_effect=[
                    playback(100),
                    playback(100, state="buffer"),
                    playback(1200, state=state, audio_format="none"),
                    playback(1200, state=state),
                    playback(1200 if state == "pause" else 1200.5, state=state),
                ]
            )
            with patch("roomcast.roku.asyncio.sleep", new=AsyncMock()):
                result = await self.roku.confirm_seek(1200, playback(state=state))
            self.assertTrue(result["confirmed"])
            self.assertEqual(result["tolerance_seconds"], 2)
            self.assertEqual(result["roku"]["state"], state)

    async def test_ignored_seek_cannot_confirm_natural_progress_through_target(self):
        clock = [0]
        real_sleep = asyncio.sleep

        async def tick(_):
            clock[0] += 0.5
            await real_sleep(0.001)

        async def status():
            return playback(100 + clock[0])

        self.roku.status = status
        with (
            patch("roomcast.roku.asyncio.sleep", side_effect=tick),
            patch("roomcast.roku.time", SimpleNamespace(monotonic=lambda: clock[0])),
            self.assertRaisesRegex(ValueError, "did not confirm"),
        ):
            await self.roku.confirm_seek(105, playback(), seconds=0.04)
        self.assertGreater(clock[0], 7)

    async def test_wrong_landing_missing_tracks_stall_and_resumed_pause_never_confirm(
        self,
    ):
        real_sleep = asyncio.sleep

        async def tick(_):
            await real_sleep(0.001)

        for before, state in [
            (playback(), playback(1190)),
            (playback(), playback(1200, video_format="none")),
            (playback(), playback(1200)),
            (playback(state="pause"), playback(1200)),
        ]:
            self.roku.status = AsyncMock(return_value=state)
            with (
                patch("roomcast.roku.asyncio.sleep", side_effect=tick),
                self.assertRaisesRegex(ValueError, "did not confirm"),
            ):
                await self.roku.confirm_seek(1200, before, seconds=0.02)

    async def test_app_change_fails_confirmation(self):
        self.roku.status = AsyncMock(return_value=playback(1200, app="other"))
        with (
            patch("roomcast.roku.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(ValueError, "app changed"),
        ):
            await self.roku.confirm_seek(1200, playback())


class ClientSeekingTests(unittest.TestCase):
    def test_timestamp_syntax(self):
        for text in ["1200", "20:00", "00:20:00"]:
            self.assertEqual(timestamp(text), 1200)
        for text in ["-1", "20:99", "20.5", "1:2:3:4", "6:00:01"]:
            with self.assertRaises(argparse.ArgumentTypeError):
                timestamp(text)

    def test_cli_translates_timestamps_without_live_socket(self):
        for args, path, expected in [
            (["play", "tv", "1", "--start", "20:00"], "/play", {"start_seconds": 1200}),
            (["seek", "--to", "20:00"], "/seek", {"mode": "absolute", "seconds": 1200}),
            (["seek", "-30"], "/seek", {"mode": "relative", "seconds": -30}),
        ]:
            with (
                patch("sys.argv", ["roomcast", *args]),
                patch("roomcast.cli.call", new=AsyncMock(return_value={})) as call,
                patch("sys.stdout", new=io.StringIO()),
            ):
                main()
                self.assertEqual(call.call_args.args[2], path)
                self.assertLessEqual(expected.items(), call.call_args.args[3].items())

    def test_player_capability_configuration(self):
        settings = config()
        settings.roku_app_id = "782875"
        settings.roku_seek_enabled = True
        with self.assertRaisesRegex(ValueError, "Stock Media Assistant"):
            settings.__post_init__()
        for app in [None, "../dev", "dev?u=x"]:
            settings.roku_app_id = app
            with self.assertRaisesRegex(ValueError, "app ID"):
                settings.__post_init__()
