import tempfile
import unittest

from roomcast.youtube import YouTube


class YouTubeTests(unittest.IsolatedAsyncioTestCase):
    async def test_seek_without_pairing_fails_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            youtube = YouTube(directory + "/auth.json")
            with self.assertRaisesRegex(ValueError, "Pair YouTube first"):
                await youtube.seek(30)
            self.assertIsNone(youtube.api)

    async def test_invalid_pairing_code_does_not_connect(self):
        youtube = YouTube("/unused")
        with self.assertRaisesRegex(ValueError, "12-digit"):
            await youtube.pair("not a code")
        self.assertIsNone(youtube.api)
