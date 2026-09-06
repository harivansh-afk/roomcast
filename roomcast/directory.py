"""Read the configured source directory as data, never executable instructions."""

import hashlib
import json
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .fetch import Fetcher, validate_url


class Links(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.table, self.link = base, 0, None
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table += 1
        if tag == "a" and self.table:
            url = urljoin(self.base, dict(attrs).get("href", ""))
            try:
                validate_url(url, None)
            except ValueError:
                return
            self.link = {"url": url, "name": ""}

    def handle_data(self, data):
        if self.link:
            self.link["name"] += data

    def handle_endtag(self, tag):
        if tag == "table":
            self.table = max(0, self.table - 1)
        if tag == "a" and self.link:
            name = " ".join(self.link["name"].split())[:100]
            url = self.link["url"]
            if name and urlsplit(url).hostname != urlsplit(self.base).hostname:
                self.items.append(
                    {
                        "id": hashlib.sha256(url.encode()).hexdigest()[:16],
                        "name": name,
                        "url": url,
                    }
                )
            self.link = None


class Directory:
    def __init__(self, url, cache=None):
        validate_url(url, None)
        self.url = url
        self.fetcher = Fetcher({urlsplit(url).hostname})
        self.items = []
        self.updated = 0
        self.cache = Path(cache) if cache else None
        if self.cache:
            try:
                saved = json.loads(self.cache.read_text())
                if saved.get("directory") == url:
                    self.items = saved.get("items", [])[:64]
            except (OSError, ValueError, AttributeError):
                pass

    async def list(self):
        if self.items and time.monotonic() - self.updated < 3600:
            return self.items
        try:
            data, final = await self.fetcher.get(self.url, {}, limit=2 * 1024 * 1024)
            parser = Links(final)
            parser.feed(data.decode("utf-8", errors="replace"))
            items = list({item["id"]: item for item in parser.items}.values())[:64]
            if not items:
                raise ValueError("Source directory returned no usable sources")
        except Exception:
            if not self.items:
                raise
            self.updated = time.monotonic() - 3540
            return self.items
        self.items = items
        self.updated = time.monotonic()
        if self.cache:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"directory": self.url, "items": self.items})
            )
            temporary.replace(self.cache)
        return self.items

    async def close(self):
        await self.fetcher.close()
