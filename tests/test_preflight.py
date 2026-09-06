import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_relay import BASE, config

from roomcast.preflight import audio_evidence, probe, video_evidence
from roomcast.relay import Session

VIDEO = dict(
    codec_type="video",
    codec_name="h264",
    profile="Main",
    width=1280,
    height=720,
    level=32,
    pix_fmt="yuv420p",
    r_frame_rate="24/1",
)
AUDIO = dict(
    codec_type="audio", codec_name="aac", profile="LC", channels=2, sample_rate="48000"
)
PLAYLIST = (
    b"#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\nsegment.ts\n#EXT-X-ENDLIST\n"
)
MASTER = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:RESOLUTION=1920x1080,AUDIO="sound"
high.m3u8
#EXT-X-STREAM-INF:RESOLUTION=1280x720,AUDIO="sound"
low.m3u8
"""


class EvidenceTests(unittest.TestCase):
    def test_reject_bad_video(self):
        for changes in (
            {"width": 2160},
            {"level": 50},
            {"pix_fmt": "yuv420p10le"},
            {"codec_name": "hevc"},
            {"width": 0},
            {"r_frame_rate": "0/0"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                video_evidence([{**VIDEO, **changes}], 1080)
        with self.assertRaises(ValueError):
            video_evidence([AUDIO], 1080)

    def test_audio_required_and_supported(self):
        for streams in (
            [VIDEO],
            [{**AUDIO, "codec_name": "flac"}],
            [{**AUDIO, "channels": 8}],
        ):
            with self.assertRaises(ValueError):
                audio_evidence(streams)


class PreflightTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, root="master.m3u8"):
        fetcher = AsyncMock()

        async def fetch(url, headers):
            name = url.rsplit("/", 1)[-1]
            if name == "master.m3u8":
                return MASTER, url
            if name.endswith(".m3u8"):
                return PLAYLIST.replace(b"segment.ts", name.encode() + b".ts"), url
            return name.encode(), url

        fetcher.get.side_effect = fetch
        self.session = Session(fetcher, config(), BASE + root, {}, "fixture")
        return self.session

    async def asyncTearDown(self):
        if hasattr(self, "session"):
            await self.session.close()

    async def test_fallback_preserves_audio_and_survives_eviction(self):
        session = self.make_session()

        async def inspect(data, executable):
            if data.startswith(b"high"):
                return [{**VIDEO, "width": 2160, "height": 1080, "level": 50}]
            if data.startswith(b"audio"):
                return [AUDIO]
            return [VIDEO]

        with (
            patch("roomcast.preflight.probe", side_effect=inspect),
            patch("roomcast.preflight.decode_frame", new=AsyncMock()),
        ):
            result = await session.prepare()
        self.assertEqual(result["video"]["width"], 1280)
        self.assertEqual(result["variants_attempted"], 2)
        self.assertTrue(result["external_audio"])
        self.assertFalse(result["visual_verified"])
        body, _ = await session.get(session.root)
        self.assertIn(b"TYPE=AUDIO", body)
        self.assertNotIn(b"1920x1080", body)
        session.cache.clear()
        session.cache_size = 0
        session.fetcher.get.side_effect = AssertionError(
            "pinned playlists must not refetch"
        )
        again, _ = await session.get(session.root)
        self.assertEqual(body, again)

    async def test_reject_incomplete_presentations_and_decode_failure(self):
        for streams in ([AUDIO], [VIDEO]):
            session = self.make_session("direct.m3u8")
            with (
                patch("roomcast.preflight.probe", new=AsyncMock(return_value=streams)),
                patch("roomcast.preflight.decode_frame", new=AsyncMock()),
            ):
                with self.assertRaises(ValueError):
                    await session.prepare()
            self.assertEqual(session.preflight["state"], "failed")
            await session.close()
        session = self.make_session("direct.m3u8")
        with (
            patch(
                "roomcast.preflight.probe", new=AsyncMock(return_value=[VIDEO, AUDIO])
            ),
            patch(
                "roomcast.preflight.decode_frame",
                new=AsyncMock(side_effect=ValueError("bad frame")),
            ),
        ):
            with self.assertRaises(ValueError):
                await session.prepare()

    async def test_compatible_muxed_source_and_probe_failure(self):
        session = self.make_session("direct.m3u8")
        with (
            patch(
                "roomcast.preflight.probe", new=AsyncMock(return_value=[VIDEO, AUDIO])
            ),
            patch("roomcast.preflight.decode_frame", new=AsyncMock()),
        ):
            evidence = await session.prepare()
        self.assertEqual(evidence["variants_attempted"], 1)
        self.assertFalse(evidence["external_audio"])
        await session.close()
        session = self.make_session("direct.m3u8")
        with patch(
            "roomcast.preflight.probe",
            new=AsyncMock(side_effect=ValueError("probe failed")),
        ):
            with self.assertRaises(ValueError):
                await session.prepare()

    async def test_stop_cancels_preflight(self):
        session = self.make_session("direct.m3u8")
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch("roomcast.preflight.probe", side_effect=blocked):
            task = asyncio.create_task(session.prepare())
            await asyncio.wait_for(entered.wait(), 1)
            await session.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(session.preflight["state"], "cancelled")


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg fixture tools unavailable",
)
class RealMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_audio_only_and_muxed_video(self):
        for video in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "sample.ts"
                args = ["ffmpeg", "-v", "error"]
                if video:
                    args += ["-f", "lavfi", "-i", "testsrc2=size=64x64:rate=24"]
                args += ["-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "1"]
                if video:
                    args += [
                        "-c:v",
                        "libx264",
                        "-profile:v",
                        "main",
                        "-level:v",
                        "3.2",
                        "-pix_fmt",
                        "yuv420p",
                        "-threads",
                        "1",
                    ]
                args += ["-c:a", "aac", "-f", "mpegts", str(target)]
                subprocess.run(args, check=True, capture_output=True, timeout=20)
                streams = await probe(target.read_bytes(), "ffprobe")
                audio_evidence(streams)
                if video:
                    self.assertEqual(video_evidence(streams, 1080)["width"], 64)
                    from roomcast.preflight import decode_frame

                    await decode_frame(target.read_bytes(), "ffmpeg")
                else:
                    with self.assertRaises(ValueError):
                        video_evidence(streams, 1080)

    async def test_invalid_probe_data(self):
        with self.assertRaises(ValueError):
            await probe(b"not media", "ffprobe")
