import unittest

from roomcast.directory import Links


class DirectoryTests(unittest.TestCase):
    def test_only_public_https_table_links_are_candidates(self):
        parser = Links("https://directory.example/")
        parser.feed(
            '<a href="https://advert.example">Ad</a><table><a href="https://site.example/">Site</a><a href="http://local.example">Bad</a><a href="https://127.0.0.1/">Private</a></table>'
        )
        self.assertEqual([row["name"] for row in parser.items], ["Site"])


class DirectoryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_directory_outage_keeps_last_known_sources(self):
        from unittest.mock import AsyncMock

        from roomcast.directory import Directory

        directory = Directory("https://directory.example/")
        directory.items = [
            {"id": "known", "name": "Known", "url": "https://site.example"}
        ]
        directory.fetcher.get = AsyncMock(side_effect=ValueError("upstream HTTP 503"))
        try:
            self.assertEqual(await directory.list(), directory.items)
        finally:
            await directory.close()

    async def test_empty_directory_keeps_last_known_sources(self):
        from unittest.mock import AsyncMock

        from roomcast.directory import Directory

        directory = Directory("https://directory.example/")
        known = [{"id": "known", "name": "Known", "url": "https://site.example"}]
        directory.items = known
        directory.fetcher.get = AsyncMock(
            return_value=(b"<html>Maintenance</html>", directory.url)
        )
        try:
            self.assertEqual(await directory.list(), known)
        finally:
            await directory.close()
