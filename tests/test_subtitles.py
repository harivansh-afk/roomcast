import asyncio
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_relay import BASE, HOST, config
from test_seeking import playback

from roomcast.cli import main
from roomcast.relay import Session
from roomcast.server import Service, errors
from roomcast.subtitles import choose, discover, language, subtitle_text, track

SRT = b"1\n00:20:00,000 --> 00:20:02,000\nHello there.\n\n"
VTT = b"WEBVTT\n\nscene\n20:00.000 --> 20:02.000 align:center\n<i>Hello there.</i>\n\n"
SEGMENT = b"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n00:20:00.000 --> 00:20:02.000\nHello there.\n\n"
MASTER = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="en.m3u8",DEFAULT=YES
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="Hindi",LANGUAGE="hi",URI="hi.m3u8"
#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="other",NAME="Wrong video",LANGUAGE="en",URI="wrong.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720,SUBTITLES="subs"
video.m3u8
"""
PLAYLIST = (
    b"#EXTM3U\n#EXT-X-TARGETDURATION:1800\n#EXTINF:1800,\nen.vtt\n#EXT-X-ENDLIST\n"
)


def english(kind="file"):
    return track(BASE + "en.vtt", "English", "en", kind, [HOST])


class SubtitleTextTests(unittest.TestCase):
    def test_srt_and_webvtt_files_keep_full_episode_timestamps(self):
        for data in (SRT, VTT):
            result, mime = subtitle_text(data)
            self.assertEqual(result, SRT)
            self.assertEqual(mime, "application/x-subrip")
            self.assertIn(b"00:20:00,000", result)

    def test_segmented_webvtt_preserves_timestamp_map_and_empty_segments(self):
        self.assertEqual(subtitle_text(SEGMENT, segmented=True), (SEGMENT, "text/vtt"))
        self.assertEqual(subtitle_text(b"WEBVTT\n\n", segmented=True)[0], b"WEBVTT\n\n")
        with self.assertRaisesRegex(ValueError, "must remain"):
            subtitle_text(SEGMENT)

    def test_bad_subtitles_rejected(self):
        for data in (
            b"<html>login required</html>",
            b"WEBVTTfoo",
            b"WEBVTT\n\n",
            b"\xff",
            b"x" * (2 * 1024 * 1024 + 1),
            b"1\n00:20:03,000 --> 00:20:02,000\nwrong\n",
            b"1\n00:99:00,000 --> 00:99:02,000\nwrong\n",
        ):
            with self.subTest(data=data[:50]), self.assertRaises(ValueError):
                subtitle_text(data)
        with self.assertRaisesRegex(ValueError, "timestamp map"):
            subtitle_text(
                SEGMENT.replace(b"MPEGTS:900000", b"MPEGTS:bad"), segmented=True
            )

    def test_default_prefers_full_english_with_available_fallback(self):
        es = track(BASE + "es.srt", "Spanish", "es", "file", [HOST], default=True)
        forced = {**english(), "forced": True}
        full = {**english(), "language": "en-us"}
        self.assertIs(choose([es, forced, full], "en"), full)
        self.assertIs(choose([es], "en"), es)
        self.assertIsNone(choose([], "en"))
        self.assertEqual(language("ENG"), "en")
        self.assertEqual(language("en-US"), "en-us")

    def test_cli_subtitle_controls(self):
        for args, method, data in [
            (["subtitles"], "GET", None),
            (["subtitles", "on"], "POST", {"enabled": True}),
            (["subtitles", "off"], "POST", {"enabled": False}),
            (["subtitles", "--language", "hi"], "POST", {"language": "hi"}),
        ]:
            with (
                patch("sys.argv", ["roomcast", *args]),
                patch("roomcast.cli.call", new=AsyncMock(return_value={})) as call,
                patch("sys.stdout", new=io.StringIO()),
            ):
                main()
                self.assertEqual(call.call_args.args[1:4], (method, "/subtitles", data))


class SubtitleRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fetcher = AsyncMock()
        self.session = Session(
            self.fetcher, config(), BASE + "master.m3u8", {}, "episode"
        )
        self.session.selections[BASE + "master.m3u8"] = 4

    async def asyncTearDown(self):
        await self.session.close()

    async def test_selected_hls_subtitle_group_and_timestamp_map_survive_relay(self):
        body, _ = self.session.rewrite(MASTER, BASE + "master.m3u8")
        self.assertIn(b'SUBTITLES="subs"', body)
        self.assertNotIn(b"Wrong video", body)
        self.assertNotIn(b"https://", body)
        self.assertEqual(
            [t["language"] for t in self.session.subtitle_tracks], ["en", "hi"]
        )
        self.fetcher.get.side_effect = [
            (PLAYLIST, BASE + "en.m3u8"),
            (SEGMENT, BASE + "en.vtt"),
        ]
        key = next(
            key
            for key, value in self.session.resources.items()
            if value.url == BASE + "en.m3u8"
        )
        playlist, mime = await self.session.get(key)
        self.assertEqual(mime, "application/vnd.apple.mpegurl")
        segment_key = next(
            line.rsplit(b"/", 1)[-1].decode()
            for line in playlist.splitlines()
            if line and not line.startswith(b"#")
        )
        self.assertEqual(await self.session.get(segment_key), (SEGMENT, "text/vtt"))
        self.assertEqual(self.session.metrics["segments_remuxed"], 0)
        self.assertNotIn("segments_verified", self.session.metrics)

    async def test_invalid_caption_url_does_not_break_video_or_leave_dangling_group(
        self,
    ):
        modified = MASTER.replace(b"en.m3u8", b"http://127.0.0.1/a").replace(
            b"hi.m3u8", b"https://127.0.0.1/b"
        )
        body, _ = self.session.rewrite(modified, BASE + "master.m3u8")
        self.assertNotIn(b'SUBTITLES="subs"', body)
        self.assertEqual(self.session.subtitle_tracks, [])
        self.assertIsNotNone(self.session.subtitle_error)
        self.assertIsNone(self.session.failure)

    async def test_sidecar_conversion_is_bounded_and_cached(self):
        self.fetcher.get.return_value = VTT, BASE + "en.vtt"
        key = self.session.register(BASE + "en.vtt", role="subtitle-file")
        self.assertEqual(await self.session.get(key), (SRT, "application/x-subrip"))
        self.assertEqual(await self.session.get(key), (SRT, "application/x-subrip"))
        self.fetcher.get.assert_awaited_once_with(
            BASE + "en.vtt", {}, limit=2 * 1024 * 1024
        )

    async def test_discovery_uses_only_the_video_frame_and_rejects_private_urls(self):
        frame = SimpleNamespace(
            url=BASE + "player",
            locator=lambda _: SimpleNamespace(
                evaluate_all=AsyncMock(
                    return_value=[
                        {"url": "en.vtt", "name": "English", "language": "en"},
                        {
                            "url": "http://127.0.0.1/private",
                            "name": "bad",
                            "language": "en",
                        },
                    ]
                )
            ),
        )
        tracks = await discover(frame, [HOST])
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["url"], BASE + "en.vtt")
        self.assertEqual(await discover(None, [HOST]), [])


class SubtitleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = Service(config(roku_app_id="dev", roku_subtitle_control=True))
        self.service.network = SimpleNamespace(
            address="127.0.0.1", local_address="10.0.0.1", ensure=AsyncMock()
        )
        self.service.roku.request = AsyncMock(
            side_effect=AssertionError("unexpected device IO")
        )
        self.service.roku.status = AsyncMock(return_value=playback())
        self.service.roku.command = AsyncMock(return_value={"state": "stop"})
        self.service.roku.subtitles = AsyncMock()
        self.service.fetcher.get = AsyncMock(return_value=(VTT, BASE + "en.vtt"))
        session = Session(
            self.service.fetcher,
            self.service.config,
            BASE + "video.m3u8",
            {},
            "Episode",
        )
        session.subtitle_tracks = [
            english(),
            track(BASE + "hi.srt", "Hindi", "hi", "file", [HOST]),
        ]
        self.service.session = session
        app = web.Application(middlewares=[errors], client_max_size=4096)
        app.add_routes(
            [
                web.get("/subtitles", self.service.subtitles),
                web.post("/subtitles", self.service.subtitles),
                web.post("/subtitle-state/{token}", self.service.subtitle_report),
                web.get("/media/{token}/{key}", self.service.media),
                web.post("/play", self.service.start_play),
                web.post("/command/{command}", self.service.command),
            ]
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.service.close()

    async def report(self, params, **changes):
        return await self.client.post(
            f"/subtitle-state/{self.service.session.token}",
            json={
                "request": params["subtitleRequest"],
                "track": params["subtitleId"],
                "enabled": params["subtitlesEnabled"] == "true",
                "applied": True,
                "available": bool(params["subtitleId"]),
                "sequence": 1,
                "caption_mode": "On" if params["subtitlesEnabled"] == "true" else "Off",
                **changes,
            },
        )

    async def test_read_lists_available_tracks_without_device_io_or_upstream_urls(self):
        response = await self.client.get("/subtitles")
        data = await response.json()
        self.assertTrue(data["default_enabled"])
        self.assertEqual(data["preferred_language"], "en")
        self.assertEqual(len(data["available"]), 2)
        self.assertNotIn("https://", json.dumps(data))
        self.service.roku.status.assert_not_awaited()

    async def test_language_and_off_controls_require_player_acknowledgement(self):
        async def apply(params):
            response = await self.report(params)
            self.assertEqual(response.status, 204)

        self.service.roku.subtitles.side_effect = apply
        for body in [{"language": "hi"}, {"enabled": False}, {"enabled": True}]:
            response = await self.client.post("/subtitles", json=body)
            self.assertEqual(response.status, 200, await response.text())
            data = await response.json()
            self.assertTrue(data["confirmed"])
            self.assertTrue(data["player"]["applied"])
            self.assertEqual(
                data["requested"]["track"],
                self.service.session.subtitle_tracks[1]["id"],
            )
        self.assertTrue(self.service.config.subtitles_enabled)

    async def test_invalid_inputs_and_missing_tracks_send_no_commands(self):
        for body in [
            [],
            {},
            {"enabled": "true"},
            {"language": "English"},
            {"track_id": "bad"},
            {"language": "hi", "track_id": "a" * 16},
            {"language": "fr"},
            {"url": BASE + "bad.srt"},
        ]:
            response = await self.client.post("/subtitles", json=body)
            self.assertEqual(response.status, 400, body)
        self.service.roku.subtitles.assert_not_awaited()
        self.service.roku.status.assert_not_awaited()

    async def test_unsupported_player_rejected_without_device_io(self):
        self.service.config.roku_subtitle_control = False
        response = await self.client.post("/subtitles", json={"enabled": True})
        self.assertEqual(response.status, 400)
        self.service.roku.status.assert_not_awaited()

    async def test_stale_foreign_and_malformed_player_reports_rejected(self):
        session = self.service.session
        params = self.service.subtitle_params(session, True, session.subtitle_tracks[0])
        response = await self.report(params, request="old")
        self.assertEqual(response.status, 409)
        response = await self.report(params, available=False)
        self.assertEqual(response.status, 400)
        response = await self.report(params, enabled="true")
        self.assertEqual(response.status, 400)
        self.service.network.address = "10.0.0.2"
        response = await self.report(params)
        self.assertEqual(response.status, 404)
        self.assertIsNone(session.subtitle_report)
        self.assertFalse(session.subtitle_event.is_set())

    async def test_stop_cancels_waiting_caption_change(self):
        entered = asyncio.Event()

        async def sent(params):
            entered.set()

        self.service.roku.subtitles.side_effect = sent
        request = asyncio.create_task(
            self.client.post("/subtitles", json={"enabled": True})
        )
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIsNone(self.service.session.subtitle_report)
        response = await self.client.post("/command/stop")
        self.assertEqual(response.status, 200)
        response = await asyncio.wait_for(request, 1)
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.service.session)

    async def test_sent_command_without_native_confirmation_fails(self):
        self.service.session.subtitle_event.wait = AsyncMock(side_effect=TimeoutError)
        response = await self.client.post("/subtitles", json={"enabled": True})
        self.assertEqual(response.status, 400)
        self.assertIn("not confirmed", await response.text())
        self.service.roku.subtitles.assert_awaited_once()
        self.assertIsNone(self.service.session.subtitle_report)

    async def test_delayed_success_cannot_overwrite_newer_native_change(self):
        session = self.service.session
        params = self.service.subtitle_params(session, True, session.subtitle_tracks[0])
        response = await self.report(
            params, applied=False, caption_mode="Off", sequence=2
        )
        self.assertEqual(response.status, 204)
        response = await self.report(params, sequence=1)
        self.assertEqual(response.status, 409)
        self.assertFalse(session.subtitle_report["applied"])
        self.assertEqual(session.subtitle_report["caption_mode"], "Off")

    async def test_bad_caption_does_not_mark_video_delivery_failed(self):
        self.service.fetcher.get.return_value = (
            b"<html>unavailable</html>",
            BASE + "en.vtt",
        )
        session = self.service.session
        key = session.register(BASE + "en.vtt", role="subtitle-file")
        response = await self.client.get(f"/media/{session.token}/{key}")
        self.assertEqual(response.status, 502)
        self.assertIsNotNone(session.subtitle_error)
        self.assertIsNone(session.failure)

    async def test_new_episode_requests_subtitles_on_by_default_with_complete_timeline(
        self,
    ):
        async def sources(*args, **kwargs):
            yield {
                "title": "Episode",
                "provider": "fixture",
                "sources": [BASE + "video.m3u8"],
                "headers": {},
                "subtitles": {BASE + "video.m3u8": [english()]},
            }

        self.service.resolver.resolve = sources
        self.service.prepare = AsyncMock()
        self.service.roku.launch = AsyncMock()
        self.service.roku.confirm = AsyncMock(return_value=playback())
        response = await self.client.post(
            "/play", json={"kind": "tv", "id": 1, "replace": True}
        )
        self.assertEqual(response.status, 202)
        await self.service.job
        params = self.service.roku.launch.call_args.kwargs["subtitles"]
        self.assertEqual(params["subtitlesEnabled"], "true")
        self.assertEqual(params["subtitleId"], english()["id"])
        self.assertEqual(len(json.loads(params["subtitleTracks"])), 1)
        self.assertNotIn("https://", json.dumps(params))
        session = self.service.session
        key = params["subtitleUrl"].rsplit("/", 1)[-1]
        data, _ = await session.get(key)
        self.assertIn(b"00:20:00,000", data)
        self.assertEqual(self.service.job_state["state"], "playing")
