import ipaddress
import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .fetch import validate_url


@dataclass
class Config:
    roku_ip: str
    roku_serial: str
    public_base: str
    control_socket: str = "/run/roomcast/control.sock"
    media_host: str = "127.0.0.1"
    media_port: int = 18796
    chromium: str = "chromium"
    ffmpeg: str = "ffmpeg"
    max_height: int = 1080
    cache_bytes: int = 128 * 1024 * 1024
    session_seconds: int = 21600
    site_url: str = "https://cinejoy.to"
    allowed_hosts: list[str] | None = None

    def __post_init__(self):
        validate_url(self.site_url, None)
        site = urlsplit(self.site_url)
        if site.path not in ("", "/") or site.query:
            raise ValueError("site_url must be an HTTPS origin")
        self.site_url = self.site_url.rstrip("/")
        ip = ipaddress.ip_address(self.roku_ip)
        if ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified:
            raise ValueError("roku_ip must identify a LAN device")
        url = urlsplit(self.public_base)
        if (
            url.scheme != "http"
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("public_base must be an HTTP LAN origin")
        address = ipaddress.ip_address(url.hostname)
        if (
            address.version != 4
            or not address.is_private
            or address.is_loopback
            or address.is_unspecified
        ):
            raise ValueError("public_base must use a LAN IPv4 address")
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("invalid public media port")
        if not 1 <= self.media_port <= 65535:
            raise ValueError("invalid backend media port")
        if self.media_host != "127.0.0.1":
            raise ValueError("media server binds loopback; use a restricted LAN proxy")
        if self.max_height not in (720, 1080) or self.cache_bytes < 1048576:
            raise ValueError("invalid height or cache limit")

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))
