import asyncio
import unittest
from unittest.mock import AsyncMock

from roomcast.config import Config
from roomcast.fetch import validate_url, PublicResolver
from roomcast.relay import Session


HOST = "media.example.com"
BASE = "https://media.example.com/"


def config(**kw):
    return Config(roku_ip="10.0.0.2", roku_serial="test", public_base="http://10.0.0.1:18796",
                  allowed_hosts=[HOST], **kw)


class SecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_url_boundary(self):
        validate_url(BASE + "a.m3u8", [HOST])
        for url in ["http://media.example.com/a", "https://127.0.0.1/a", "https://media.example.com.evil/a",
                    "https://user:pass@media.example.com/a", "https://media.example.com:8443/a",
                    "file:///etc/passwd", "https://media.example.com/a#x"]:
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
        source = b'''#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=9000000,RESOLUTION=3840x2160
4k.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720.m3u8
'''
        rewritten, segments = self.session.rewrite(source, BASE + "master.m3u8")
        self.assertNotIn(b"2160", rewritten)
        self.assertNotIn(b"1280", rewritten)
        self.assertIn(b"1920", rewritten)
        self.assertNotIn(BASE.encode(), rewritten)
        self.assertEqual(segments, [])
        self.assertIn(BASE + "audio.m3u8", [r.url for r in self.session.resources.values()])

    def test_fragmented_segments_remember_init_and_normalize_urls(self):
        source = b'''#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4,
part.jpg
#EXTINF:4,
part2.png
#EXT-X-ENDLIST
'''
        rewritten, segments = self.session.rewrite(source, BASE + "720p/playlist.jpg")
        self.assertNotIn(b"EXT-X-MAP", rewritten)
        self.assertEqual(len(segments), 2)
        self.assertEqual(self.session.resources[segments[0]].init, BASE + "720p/init.mp4")
        self.assertNotIn(b".jpg", rewritten)

    def test_nested_origin_escape_and_encryption_rejected(self):
        for line in ["https://evil.example/video", "http://127.0.0.1:8060/query/apps",
                     '#EXT-X-KEY:METHOD=AES-128,URI="key"', '#EXT-X-BYTERANGE:123']:
            with self.subTest(line=line), self.assertRaises(ValueError):
                self.session.rewrite(("#EXTM3U\n" + line + "\n").encode(), BASE)

    def test_live_playlist_rejected_instead_of_caching_forever(self):
        with self.assertRaises(ValueError):
            self.session.rewrite(b"#EXTM3U\n#EXTINF:4,\na.ts\n", BASE)

    async def test_concurrent_read_is_fetched_once(self):
        async def fetch(*args):
            await asyncio.sleep(.01)
            return b"data", BASE + "a.ts"
        self.fetch.get.side_effect = fetch
        key = self.session.register(BASE + "a.ts")
        results = await asyncio.gather(*[self.session.get(key) for _ in range(5)])
        self.assertTrue(all(result[0] == b"data" for result in results))
        self.fetch.get.assert_awaited_once()

    async def test_failed_fetch_can_be_retried(self):
        self.fetch.get.side_effect = [ValueError("upstream HTTP 503"), (b"data", BASE + "a.ts")]
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


if __name__ == "__main__":
    unittest.main()
