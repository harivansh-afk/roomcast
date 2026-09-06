import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .fetch import validate_url


@dataclass
class Config:
    roku_ip: str | None
    roku_serial: str
    roku_app_id: str = "782875"
    roku_seek_enabled: bool = False
    public_base: str = "http://127.0.0.1:18795"
    lan_interface: str | None = None
    lan_port: int = 18795
    discovery_port: int = 18794
    discovery_networks: list[str] = field(default_factory=list)
    roku_mac: str | None = None
    youtube_auth: str = "/var/lib/roomcast/youtube.json"
    address_cache: str = "/var/lib/roomcast/address.json"
    ip_command: str = "ip"
    control_socket: str = "/run/roomcast/control.sock"
    media_host: str = "127.0.0.1"
    media_port: int = 18796
    chromium: str = "chromium"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_height: int = 1080
    cache_bytes: int = 128 * 1024 * 1024
    session_seconds: int = 21600
    directory_cache: str | None = None
    directory_url: str = "https://www.bestfreestreaming.org/"
    site_url: str = "https://cinejoy.to"
    allowed_hosts: list[str] | None = None

    def __post_init__(self):
        if not isinstance(self.roku_app_id, str) or not re.fullmatch(
            r"(?:[0-9]+|dev)", self.roku_app_id
        ):
            raise ValueError("invalid Roku player app ID")
        if type(self.roku_seek_enabled) is not bool:
            raise ValueError("roku_seek_enabled must be a boolean")
        if self.roku_seek_enabled and self.roku_app_id == "782875":
            raise ValueError(
                "Stock Media Assistant does not support timestamp commands; install the Roomcast player first"
            )
        networks = [ipaddress.IPv4Network(n) for n in self.discovery_networks]
        if sum(n.num_addresses for n in networks) > 1024 or any(
            not n.is_private for n in networks
        ):
            raise ValueError(
                "discovery networks must be private and total at most 1024 addresses"
            )
        validate_url(self.site_url, None)
        site = urlsplit(self.site_url)
        if site.path not in ("", "/") or site.query:
            raise ValueError("site_url must be an HTTPS origin")
        self.site_url = self.site_url.rstrip("/")
        ip = ipaddress.ip_address(self.roku_ip) if self.roku_ip else None
        if ip is not None and (
            ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_unspecified
        ):
            raise ValueError("roku_ip must identify a LAN device")
        if ip is None and not self.lan_interface:
            raise ValueError("roku_ip is required without LAN discovery")
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
            or (address.is_loopback and not self.lan_interface)
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
