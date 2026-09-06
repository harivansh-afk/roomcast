import argparse
import asyncio
import json
import os
import sys

import aiohttp


async def call(socket, method, path, data=None, params=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket),
                                     timeout=aiohttp.ClientTimeout(total=90)) as client:
        async with client.request(method, "http://localhost" + path, json=data, params=params) as response:
            text = await response.text()
            if response.status >= 400:
                raise ValueError(text)
            return json.loads(text)


def main():
    parser = argparse.ArgumentParser(description="Control the shared Roku through Roomcast")
    parser.add_argument("--socket", default=os.environ.get("ROOMCAST_SOCKET", "/run/roomcast/control.sock"))
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    search = sub.add_parser("search")
    search.add_argument("query")
    play = sub.add_parser("play")
    play.add_argument("kind", choices=["tv", "movie"])
    play.add_argument("id", type=int)
    play.add_argument("--season", type=int, default=1)
    play.add_argument("--episode", type=int, default=1)
    play.add_argument("--replace", action="store_true")
    for action in ("stop", "pause", "resume", "home", "volume_up", "volume_down", "mute", "power_on"):
        sub.add_parser(action)
    args = parser.parse_args()
    if args.action == "play":
        method, path, data, params = "POST", "/play", {
            key: getattr(args, key) for key in ("kind", "id", "season", "episode", "replace")}, None
    elif args.action == "search":
        method, path, data, params = "GET", "/search", None, {"q": args.query}
    elif args.action == "status":
        method, path, data, params = "GET", "/status", None, None
    else:
        method, path, data, params = "POST", "/command/" + args.action, {}, None
    try:
        print(json.dumps(asyncio.run(call(args.socket, method, path, data, params)), indent=2))
    except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
