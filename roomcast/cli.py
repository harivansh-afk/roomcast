import argparse
import asyncio
import getpass
import json
import re
import sys
from typing import get_args

import aiohttp

from .client import Command, Kind, call, default_socket


def timestamp(value):
    if not re.fullmatch(r"\d+(?::[0-5]\d){0,2}", value):
        raise argparse.ArgumentTypeError("use seconds, MM:SS or HH:MM:SS")
    result = 0
    for part in value.split(":"):
        result = result * 60 + int(part)
    if result > 21600:
        raise argparse.ArgumentTypeError("timestamp must be within six hours")
    return result


def main():
    parser = argparse.ArgumentParser(
        description="Control the shared Roku through Roomcast"
    )
    parser.add_argument("--socket", default=default_socket())
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status")
    sub.add_parser("sources")
    sub.add_parser("pair-youtube")
    subtitles = sub.add_parser("subtitles")
    subtitles.add_argument("mode", nargs="?", choices=("on", "off"))
    subtitle_track = subtitles.add_mutually_exclusive_group()
    subtitle_track.add_argument("--language")
    subtitle_track.add_argument("--track", dest="track_id")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--source", default="cinejoy")
    seek = sub.add_parser("seek")
    seek.add_argument("seconds", type=int, nargs="?")
    seek.add_argument(
        "--to", type=timestamp, help="absolute seconds, MM:SS or HH:MM:SS"
    )
    play = sub.add_parser("play")
    play.add_argument("kind", choices=get_args(Kind))
    play.add_argument("id")
    play.add_argument("--season", type=int, default=1)
    play.add_argument("--episode", type=int, default=1)
    play.add_argument("--replace", action="store_true")
    play.add_argument("--source", default="cinejoy")
    play.add_argument("--start", dest="start_seconds", type=timestamp, default=0)
    for action in get_args(Command):
        sub.add_parser(action)
    args = parser.parse_args()
    if args.action == "play":
        if args.kind in ("tv", "movie"):
            try:
                args.id = int(args.id)
            except ValueError:
                parser.error("movie and TV IDs must be integers")
        method, path, data, params = (
            "POST",
            "/play",
            {
                key: getattr(args, key)
                for key in (
                    "kind",
                    "id",
                    "season",
                    "episode",
                    "replace",
                    "source",
                    "start_seconds",
                )
            },
            None,
        )
    elif args.action == "seek":
        if (args.seconds is None) == (args.to is None):
            parser.error("seek requires either relative seconds or --to TIMESTAMP")
        method, path, data, params = (
            "POST",
            "/seek",
            {
                "seconds": args.seconds if args.to is None else args.to,
                "mode": "relative" if args.to is None else "absolute",
            },
            None,
        )
    elif args.action == "subtitles":
        data = {
            key: getattr(args, key)
            for key in ("language", "track_id")
            if getattr(args, key) is not None
        }
        if args.mode is not None:
            data["enabled"] = args.mode == "on"
        method, path, params = "POST" if data else "GET", "/subtitles", None
        data = data or None
    elif args.action == "search":
        method, path, data, params = (
            "GET",
            "/search",
            None,
            {"q": args.query, "source": args.source},
        )
    elif args.action == "pair-youtube":
        method, path, data, params = (
            "POST",
            "/youtube/pair",
            {"code": getpass.getpass("YouTube TV code: ")},
            None,
        )
    elif args.action == "sources":
        method, path, data, params = "GET", "/sources", None, None
    elif args.action == "status":
        method, path, data, params = "GET", "/status", None, None
    else:
        method, path, data, params = "POST", "/command/" + args.action, {}, None
    try:
        print(
            json.dumps(
                asyncio.run(call(args.socket, method, path, data, params)), indent=2
            )
        )
    except (ValueError, OSError, aiohttp.ClientError, asyncio.TimeoutError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        sys.exit(1)
