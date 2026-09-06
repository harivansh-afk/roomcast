import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from roomcast.fetch import Fetcher, TemporaryUpstreamError


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fetcher = Fetcher(None)

    async def asyncTearDown(self):
        await self.fetcher.close()

    async def test_transient_failure_retries_but_policy_rejection_does_not(self):
        self.fetcher._get = AsyncMock(
            side_effect=[TemporaryUpstreamError(), TimeoutError(), (b"media", "url")]
        )
        with patch("roomcast.fetch.asyncio.sleep", new=AsyncMock()):
            self.assertEqual(await self.fetcher.get("url", {}), (b"media", "url"))
        self.assertEqual(self.fetcher._get.await_count, 3)
        self.fetcher._get = AsyncMock(
            side_effect=ValueError("media origin is not allowed")
        )
        with self.assertRaises(ValueError):
            await self.fetcher.get("url", {})
        self.fetcher._get.assert_awaited_once()

    async def test_retries_are_bounded_and_cancellation_propagates(self):
        self.fetcher._get = AsyncMock(side_effect=TemporaryUpstreamError())
        with (
            patch("roomcast.fetch.asyncio.sleep", new=AsyncMock()),
            self.assertRaisesRegex(ValueError, "three attempts"),
        ):
            await self.fetcher.get("url", {})
        self.assertEqual(self.fetcher._get.await_count, 3)
        self.fetcher._get = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await self.fetcher.get("url", {})
        self.fetcher._get.assert_awaited_once()
