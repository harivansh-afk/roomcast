"""Media must use public HTTPS endpoints; deployments may further restrict hosts."""

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver


class PublicResolver(AbstractResolver):
    def __init__(self):
        self.inner = aiohttp.resolver.ThreadedResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        answers = await self.inner.resolve(host, port, family)
        if not answers or any(
            not ipaddress.ip_address(a["host"]).is_global for a in answers
        ):
            raise ValueError("media DNS returned a non-public address")
        return answers

    async def close(self):
        await self.inner.close()


def validate_url(url, allowed_hosts):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or (allowed_hosts is not None and parsed.hostname not in allowed_hosts)
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("media origin is not allowed")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    raise ValueError("literal media IPs are not allowed")


class Fetcher:
    def __init__(self, hosts):
        self.hosts = frozenset(hosts) if hosts is not None else None
        self.client = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=PublicResolver(), limit=8),
            timeout=aiohttp.ClientTimeout(total=25, connect=8),
            trust_env=False,
        )

    async def get(self, url, headers, limit=32 * 1024 * 1024):
        for _ in range(5):
            validate_url(url, self.hosts)
            async with self.client.get(
                url, headers=headers, allow_redirects=False
            ) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status != 200:
                    raise ValueError(f"upstream HTTP {response.status}")
                if response.content_length and response.content_length > limit:
                    raise ValueError("upstream object exceeds limit")
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("upstream object exceeds limit")
                    chunks.append(chunk)
                return b"".join(chunks), url
        raise ValueError("too many upstream redirects")

    async def close(self):
        await self.client.close()
