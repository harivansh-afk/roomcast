import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


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
    allowed_hosts: list[str] = field(default_factory=lambda: [
        "info.movieboxnoob.cc", "lol.movieboxnoob.cc", "nebula.bright67.online",
    ])

    def __post_init__(self):
        ip = ipaddress.ip_address(self.roku_ip)
        if not ip.is_private or ip.is_loopback or ip.is_unspecified:
            raise ValueError("roku_ip must identify a LAN device")
        url = urlsplit(self.public_base)
        if url.scheme != "http" or not url.hostname or url.username or url.path not in ("", "/"):
            raise ValueError("public_base must be an HTTP LAN origin")
        if not ipaddress.ip_address(url.hostname).is_private:
            raise ValueError("public_base must use a LAN IP")
        if self.media_host != "127.0.0.1":
            raise ValueError("media server binds loopback; use a restricted LAN proxy")
        if self.max_height not in (720, 1080) or self.cache_bytes < 1048576:
            raise ValueError("invalid height or cache limit")

    @classmethod
    def load(cls, path):
        return cls(**json.loads(Path(path).read_text()))
