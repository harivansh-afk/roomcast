import unittest
from types import SimpleNamespace

from roomcast.browser import Browser


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_arbitrary_actions_are_rejected(self):
        browser = Browser(SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "unsupported"):
            await browser.act("https://example.com", "execute_javascript")

    async def test_unknown_capture_is_rejected(self):
        browser = Browser(SimpleNamespace())
        with self.assertRaisesRegex(ValueError, "expired"):
            await anext(browser.selected("invented"))

    def test_capture_excludes_credentials_and_private_urls(self):
        browser = Browser(SimpleNamespace(config=SimpleNamespace(allowed_hosts=None)))
        request = SimpleNamespace(
            headers={
                "cookie": "private",
                "authorization": "private",
                "referer": "https://site.example/",
                "user-agent": "Browser",
            }
        )
        for url in ("https://127.0.0.1/video.m3u8", "https://cdn.example/video.m3u8"):
            browser.observe(
                SimpleNamespace(
                    status=200,
                    url=url,
                    headers={"content-type": "application/vnd.apple.mpegurl"},
                    request=request,
                )
            )
        self.assertEqual(len(browser.streams), 1)
        captured = next(iter(browser.streams.values()))
        self.assertEqual(set(captured["headers"]), {"referer", "user-agent"})
