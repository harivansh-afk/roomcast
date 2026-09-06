import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_relay import BASE, config

from roomcast.preflight import audio_evidence, decode_media, probe, video_evidence
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


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg fixture tools unavailable",
)
class RealMediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        root = Path(cls.directory.name)
        for name in ("video", "audio", "muxed"):
            args = ["ffmpeg", "-v", "error"]
            if name != "audio":
                args += ["-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24"]
            if name != "video":
                args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
            args += ["-t", "6", "-threads", "1"]
            if name != "audio":
                args += [
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "main",
                    "-level:v",
                    "3.1",
                    "-pix_fmt",
                    "yuv420p",
                    "-g",
                    "24",
                    "-sc_threshold",
                    "0",
                ]
            if name != "video":
                args += ["-c:a", "aac", "-ac", "2"]
            args += [
                "-f",
                "hls",
                "-hls_time",
                "1",
                "-hls_playlist_type",
                "vod",
                "-hls_segment_type",
                "fmp4",
                "-hls_fmp4_init_filename",
                name + "-init.mp4",
                "-hls_segment_filename",
                str(root / (name + "%d.m4s")),
                str(root / (name + ".m3u8")),
            ]
            subprocess.run(args, check=True, capture_output=True, timeout=20)
        cls.objects = {p.name: p.read_bytes() for p in root.iterdir()}
        cls.objects["master.m3u8"] = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="English",DEFAULT=YES,URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=128x72,AUDIO="sound"
video.m3u8
"""

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def asyncSetUp(self):
        self.data = dict(self.objects)

        async def fetch(url, headers, limit=32 * 1024 * 1024):
            data = self.data[url.rsplit("/", 1)[-1]]
            self.assertLessEqual(len(data), limit)
            return data, url

        self.fetcher = AsyncMock()
        self.fetcher.get.side_effect = fetch
        self.session = Session(
            self.fetcher, config(), BASE + "master.m3u8", {}, "fixture"
        )

    async def asyncTearDown(self):
        await self.session.close()

    async def test_separate_tracks_keep_initialization_and_decode_across_timeline(self):
        evidence = await self.session.prepare()
        self.assertTrue(evidence["external_audio"])
        self.assertEqual(evidence["segments_sampled"], 8)
        self.assertFalse(evidence["visual_verified"])
        for name, role in (("video", "video"), ("audio", "audio")):
            url = BASE + name + ".m3u8"
            rewritten, keys = self.session.rewrite(self.data[name + ".m3u8"], url)
            self.assertIn(b"#EXT-X-MAP:", rewritten)
            self.assertNotIn(BASE.encode(), rewritten)
            actual, mime = await self.session.get(keys[0])
            self.assertEqual(actual, self.data[name + "0.m4s"])
            self.assertEqual(mime, role + "/mp4")
            init = self.data[name + "-init.mp4"]
            decoded = await decode_media(init + actual, "ffmpeg", (role,))
            self.assertGreater(decoded[role]["frames"], 1)
        self.assertEqual(self.session.metrics["segments_remuxed"], 0)

    async def test_muxed_fragments_become_decodable_transport_stream_with_both_tracks(
        self,
    ):
        await self.session.close()
        self.session = Session(
            self.fetcher, config(), BASE + "muxed.m3u8", {}, "fixture"
        )
        evidence = await self.session.prepare()
        self.assertFalse(evidence["external_audio"])
        playlist, _ = await self.session.get(self.session.root)
        self.assertNotIn(b"#EXT-X-MAP:", playlist)
        key = next(
            line.rsplit(b"/", 1)[-1].decode()
            for line in playlist.splitlines()
            if line and not line.startswith(b"#")
        )
        data, mime = await self.session.get(key)
        self.assertEqual(mime, "video/mp2t")
        decoded = await decode_media(data, "ffmpeg", ("video", "audio"))
        self.assertEqual(set(decoded), {"video", "audio"})

    async def test_subtitles_survive_preflight_and_random_access_keeps_av_decodable(
        self,
    ):
        self.data["master.m3u8"] = self.data["master.m3u8"].replace(
            b"#EXT-X-STREAM-INF:",
            b'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="English",LANGUAGE="en",URI="subs.m3u8"\n#EXT-X-STREAM-INF:SUBTITLES="subs",',
        )
        self.data["subs.m3u8"] = (
            b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsubs.vtt\n#EXT-X-ENDLIST\n"
        )
        self.data["subs.vtt"] = (
            b"WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n00:00:03.000 --> 00:00:04.000\nHello.\n"
        )
        await self.session.prepare()
        self.assertEqual(self.session.subtitle_tracks[0]["language"], "en")
        key = self.session.register(BASE + "subs.m3u8", role="subtitle")
        playlist, _ = await self.session.get(key)
        key = next(
            line.rsplit(b"/", 1)[-1].decode()
            for line in playlist.splitlines()
            if line and not line.startswith(b"#")
        )
        self.assertEqual((await self.session.get(key))[0], self.data["subs.vtt"])
        for name in ("video", "audio"):
            _, keys = self.session.rewrite(
                self.data[name + ".m3u8"], BASE + name + ".m3u8"
            )
            for key in (keys[-2], keys[0]):
                data, _ = await self.session.get(key)
                decoded = await decode_media(
                    self.data[name + "-init.mp4"] + data, "ffmpeg", (name,)
                )
                self.assertGreater(decoded[name]["frames"], 1)

    async def test_reject_missing_audio_and_missing_video(self):
        for name in ("video", "audio"):
            await self.session.close()
            self.session = Session(
                self.fetcher, config(), BASE + name + ".m3u8", {}, "fixture"
            )
            with self.assertRaises(ValueError):
                await self.session.prepare()
            self.assertEqual(self.session.preflight["state"], "failed")

    async def test_corrupt_sampled_late_segment_rejects_before_launch(self):
        self.data["video5.m4s"] = b"broken video"
        with self.assertRaises(ValueError):
            await self.session.prepare()

    async def test_corrupt_unsampled_segment_is_not_served_and_evicted_media_is_rechecked(
        self,
    ):
        await self.session.prepare()
        _, keys = self.session.rewrite(self.data["video.m3u8"], BASE + "video.m3u8")
        self.data["video2.m4s"] = b"broken video"
        with self.assertRaises(ValueError):
            await self.session.get(keys[2])
        self.session.cache.clear()
        self.session.cache_size = 0
        self.data["video0.m4s"] = b"mutated upstream video"
        with self.assertRaises(ValueError):
            await self.session.get(keys[0])

    async def test_bad_default_audio_falls_back_to_one_usable_track(self):
        self.data["master.m3u8"] = (
            self.data["master.m3u8"].replace(b'URI="audio.m3u8"', b'URI="bad.m3u8"')
            + b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="sound",NAME="Other",URI="audio.m3u8"\n'
        )
        self.data["bad.m3u8"] = b"invalid"
        await self.session.prepare()
        master, _ = await self.session.get(self.session.root)
        self.assertEqual(master.count(b"#EXT-X-MEDIA:"), 1)
        self.assertIn(b'NAME="Other"', master)

    async def test_incompatible_high_variant_falls_back_and_pins_survive_eviction(self):
        original = self.data["master.m3u8"]
        self.data["master.m3u8"] = (
            original
            + b"#EXT-X-STREAM-INF:BANDWIDTH=4000000,RESOLUTION=1920x1080\ninvalid.m3u8\n"
        )
        self.data["invalid.m3u8"] = b"not hls"
        evidence = await self.session.prepare()
        self.assertEqual(evidence["variants_attempted"], 2)
        master, _ = await self.session.get(self.session.root)
        self.session.cache.clear()
        self.session.cache_size = 0
        self.data["master.m3u8"] = b"changed"
        again, _ = await self.session.get(self.session.root)
        self.assertEqual(master, again)

    async def test_false_codec_and_resolution_metadata_is_not_published(self):
        self.data["master.m3u8"] = self.data["master.m3u8"].replace(
            b"RESOLUTION=128x72", b'RESOLUTION=1920x1080,CODECS="avc1.4D4032,mp4a.40.2"'
        )
        await self.session.prepare()
        master, _ = await self.session.get(self.session.root)
        self.assertNotIn(b"CODECS", master)
        self.assertNotIn(b"1920x1080", master)
        self.assertIn(b"RESOLUTION=128x72", master)

    async def test_validated_initialization_is_pinned_after_cache_eviction(self):
        await self.session.prepare()
        expected = self.data["audio-init.mp4"]
        self.session.cache.clear()
        self.session.cache_size = 0
        self.data["audio-init.mp4"] = b"changed initialization"
        key = self.session.register(BASE + "audio-init.mp4")
        self.assertEqual((await self.session.get(key))[0], expected)

    async def test_audio_duration_mismatch_rejected(self):
        self.data["audio.m3u8"] = self.data["audio.m3u8"].replace(
            b"#EXTINF:1.002667,", b"#EXTINF:2.002667,"
        )
        with self.assertRaises(ValueError):
            await self.session.prepare()

    async def test_stop_cancels_validation(self):
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        with patch("roomcast.preflight.probe", side_effect=blocked):
            task = asyncio.create_task(self.session.prepare())
            await asyncio.wait_for(entered.wait(), 1)
            await self.session.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.session.preflight["state"], "cancelled")

    async def test_invalid_probe_data(self):
        with self.assertRaises(ValueError):
            await probe(b"not media", "ffprobe")
