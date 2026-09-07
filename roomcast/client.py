"""Unix-socket client shared by the CLI and MCP adapters."""

import json
import os
from typing import Literal

import aiohttp

Command = Literal[
    "stop", "pause", "resume", "home", "volume_up", "volume_down", "mute", "power_on"
]
Kind = Literal["tv", "movie", "youtube", "browser"]


def default_socket():
    return os.environ.get("ROOMCAST_SOCKET", "/run/roomcast/control.sock")


async def call(socket, method, path, data=None, params=None):
    async with aiohttp.ClientSession(
        connector=aiohttp.UnixConnector(path=socket),
        timeout=aiohttp.ClientTimeout(total=120),
    ) as client:
        async with client.request(
            method, "http://localhost" + path, json=data, params=params
        ) as response:
            text = await response.text()
            if response.status >= 400:
                raise ValueError(text)
            return json.loads(text)
