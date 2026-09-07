import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from roomcast.config import Config
from roomcast.fetch import PublicResolver, validate_url
from roomcast.relay import Session

HOST = "media.example.com"
BASE = "https://media.example.com/"


def config(**kw):
    return Config(
        roku_ip="10.0.0.2",
        roku_serial="test",
        public_base="http://10.0.0.1:18796",
        allowed_hosts=[HOST],
        **kw,
    )


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_invalid_deployment_endpoints_are_rejected(self):
        for origin in (
            "http://127.0.0.1:18795",
            "http://0.0.0.0",
            "http://10.0.0.1:0",
            "http://10.0.0.1?x=1",
            "http://10.0.0.1/#fragment",
            "http://[fd00::1]",
        ):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                replace(config(), public_base=origin)

    def test_url_boundary(self):
        validate_url(BASE + "a.m3u8", [HOST])
        for url in [
            "http://media.example.com/a",
            "https://127.0.0.1/a",
            "https://media.example.com.evil/a",
            "https://user:pass@media.example.com/a",
            "https://media.example.com:8443/a",
            "file:///etc/passwd",
            "https://media.example.com/a#x",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url, [HOST])

    async def test_dns_rebinding_private_address_rejected(self):
        resolver = PublicResolver()
        resolver.inner.resolve = AsyncMock(return_value=[{"host": "127.0.0.1"}])
        with self.assertRaises(ValueError):
            await resolver.resolve(HOST)
        await resolver.close()

    def test_no_public_control_listener(self):
        with self.assertRaises(ValueError):
            config(media_host="0.0.0.0")


class PlaylistTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fetch = AsyncMock()
        self.session = Session(self.fetch, config(), BASE + "master.m3u8", {}, "test")

    async def asyncTearDown(self):
        await self.session.close()

    def test_master_selects_height_and_rewrites_audio(self):
        source = b"""#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160
4k.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,AUDIO="a"
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720.m3u8
"""
        # Only the asynchronous preflight may choose a master rendition.
        with self.assertRaises(ValueError):
            self.session.rewrite(source, BASE + "master.m3u8")
        self.session.selections[BASE + "master.m3u8"] = 4
        self.session.audio_selections[BASE + "master.m3u8"] = 1
        rewritten, segments = self.session.rewrite(source, BASE + "master.m3u8")
        self.assertNotIn(b"2160", rewritten)
        self.assertNotIn(b"1280", rewritten)
        self.assertIn(b"1920", rewritten)
        self.assertNotIn(BASE.encode(), rewritten)
        self.assertEqual(segments, [])
        self.assertIn(
            BASE + "audio.m3u8", [r.url for r in self.session.resources.values()]
        )

    def test_fragmented_segments_remember_init_and_normalize_urls(self):
        source = b"""#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4,
part.jpg
#EXTINF:4,
part2.png
#EXT-X-ENDLIST
"""
        rewritten, segments = self.session.rewrite(source, BASE + "720p/playlist.jpg")
        self.assertIn(b"EXT-X-MAP", rewritten)
        self.assertEqual(len(segments), 2)
        self.assertEqual(
            self.session.resources[segments[0]].init, BASE + "720p/init.mp4"
        )
        self.assertNotIn(b".jpg", rewritten)

    def test_nested_origin_escape_and_encryption_rejected(self):
        for line in [
            "https://evil.example/video",
            "http://127.0.0.1:8060/query/apps",
            '#EXT-X-KEY:METHOD=AES-128,URI="key"',
            "#EXT-X-BYTERANGE:123",
        ]:
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.session.rewrite(("#EXTM3U\n" + line + "\n").encode(), BASE)

    def test_live_playlist_rejected_instead_of_caching_forever(self):
        with self.assertRaises(ValueError):
            self.session.rewrite(b"#EXTM3U\n#EXTINF:4,\na.ts\n", BASE)

    async def test_concurrent_read_is_fetched_once(self):
        async def fetch(*args):
            await asyncio.sleep(0.01)
            return b"data", BASE + "a.ts"

        self.fetch.get.side_effect = fetch
        key = self.session.register(BASE + "a.ts")
        results = await asyncio.gather(*[self.session.get(key) for _ in range(5)])
        self.assertTrue(all(result[0] == b"data" for result in results))
        self.fetch.get.assert_awaited_once()

    async def test_failed_fetch_can_be_retried(self):
        self.fetch.get.side_effect = [
            ValueError("upstream HTTP 503"),
            (b"data", BASE + "a.ts"),
        ]
        key = self.session.register(BASE + "a.ts")
        with self.assertRaises(ValueError):
            await self.session.get(key)
        self.assertEqual((await self.session.get(key))[0], b"data")

    async def test_expired_session_refuses_cached_content(self):
        self.session.created -= 22000
        with self.assertRaises(ValueError):
            await self.session.get(self.session.root)

    def test_cache_budget(self):
        self.session.config.cache_bytes = 10
        self.session.put("a", (b"12345678", "x"))
        self.session.put("b", (b"12345678", "x"))
        self.assertEqual(self.session.cache_size, 8)
        self.assertNotIn("a", self.session.cache)

    async def test_raw_init_fetch_is_shared_and_each_caller_enforces_its_limit(self):
        async def fetch(*args, **kwargs):
            await asyncio.sleep(0.01)
            return b"initialization", BASE + "init.mp4"

        self.fetch.get.side_effect = fetch
        results = await asyncio.gather(
            self.session.raw(BASE + "init.mp4", 100),
            self.session.raw(BASE + "init.mp4", 2),
            return_exceptions=True,
        )
        self.assertEqual(results[0][0], b"initialization")
        self.assertIsInstance(results[1], ValueError)
        self.fetch.get.assert_awaited_once()
        with self.assertRaises(ValueError):
            await self.session.raw(BASE + "init.mp4", 2)

    async def test_prefetch_is_bounded_leaves_demand_capacity_and_stops_cleanly(self):
        entered = asyncio.Event()
        active = 0

        async def fetch(url, *args, **kwargs):
            nonlocal active
            if not url.endswith("demand"):
                active += 1
                if active == 2:
                    entered.set()
                await asyncio.Event().wait()
            return b"data", url

        self.fetch.get.side_effect = fetch
        keys = [self.session.register(BASE + str(i)) for i in range(20)]
        self.session.prefetch(keys)
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual(len(self.session.prefetches), 4)
        self.assertEqual(active, 2)
        demand = self.session.register(BASE + "demand")
        self.assertEqual(
            (await asyncio.wait_for(self.session.get(demand), 1))[0], b"data"
        )
        await self.session.close()
        self.assertFalse(self.session.pending)
        self.assertFalse(self.session.raw_pending)
        self.assertFalse(self.session.prefetches)

    async def test_failed_shared_work_is_owned_after_its_waiter_disconnects(self):
        entered, release = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))

        async def fetch(*args):
            entered.set()
            await release.wait()
            raise ValueError("upstream failed after client disconnect")

        self.fetch.get.side_effect = fetch
        try:
            request = asyncio.create_task(self.session.get(self.session.root))
            await asyncio.wait_for(entered.wait(), 1)
            request.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request
            release.set()
            for _ in range(20):
                await asyncio.sleep(0)
            self.assertFalse(self.session.pending)
            self.assertFalse(self.session.raw_pending)
            self.assertFalse(unhandled)
        finally:
            loop.set_exception_handler(previous)


if __name__ == "__main__":
    unittest.main()
