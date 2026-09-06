"""Paired YouTube TV control; credentials remain private mutable service state."""

import asyncio
import json
import logging
import os
from pathlib import Path

from pyytlounge import YtLoungeApi


class YouTube:
    def __init__(self, path):
        self.path = Path(path)
        self.api = None
        self.subscription = None

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(self.api.store_auth_state(), file)
        temporary.replace(self.path)

    async def start(self):
        if self.api is None:
            self.api = await YtLoungeApi(
                "Roomcast", logger=logging.getLogger("roomcast.youtube")
            ).__aenter__()

    async def pair(self, code):
        if (
            not isinstance(code, str)
            or not code.replace(" ", "").isdigit()
            or len(code.replace(" ", "")) != 12
        ):
            raise ValueError("expected the 12-digit YouTube TV code")
        await self.close()
        await self.start()
        async with asyncio.timeout(20):
            if not await self.api.pair(code.replace(" ", "")):
                raise ValueError("YouTube pairing failed; obtain a fresh TV code")
        self.save()
        return {"paired": True}

    async def connect(self):
        if not self.path.exists():
            raise ValueError(
                "Pair YouTube first: TV Settings > Link with TV code, then roomcast pair-youtube"
            )
        await self.start()
        if self.api.connected() and self.subscription and not self.subscription.done():
            return
        async with asyncio.timeout(20):
            self.api.load_auth_state(json.loads(self.path.read_text()))
            if not await self.api.refresh_auth() or not await self.api.connect():
                raise ValueError(
                    "YouTube TV connection unavailable; open YouTube on the paired TV"
                )
        self.save()
        if self.subscription:
            self.subscription.cancel()
            await asyncio.gather(self.subscription, return_exceptions=True)
        self.subscription = asyncio.create_task(self.api.subscribe())

    async def seek(self, target):
        await self.connect()
        async with asyncio.timeout(10):
            if not await self.api.seek_to(target):
                raise ValueError("YouTube rejected seek")

    async def close(self):
        if self.subscription:
            self.subscription.cancel()
            await asyncio.gather(self.subscription, return_exceptions=True)
            self.subscription = None
        if self.api:
            await self.api.close()
            self.api = None
