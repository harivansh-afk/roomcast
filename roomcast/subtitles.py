"""Provider subtitle discovery and bounded, text-only subtitle delivery."""

import hashlib
import re
from urllib.parse import urljoin

from .fetch import validate_url

MAX_SUBTITLE_BYTES = 2 * 1024 * 1024
LANGUAGES = {
    "eng": "en",
    "hin": "hi",
    "spa": "es",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "ita": "it",
    "por": "pt",
    "jpn": "ja",
    "kor": "ko",
    "chi": "zh",
    "zho": "zh",
    "ara": "ar",
    "rus": "ru",
}


def language(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value
    ):
        raise ValueError(
            "subtitle language must be a language code such as en or en-US"
        )
    parts = value.lower().split("-")
    parts[0] = LANGUAGES.get(parts[0], parts[0])
    return "-".join(parts)


def track(url, name, lang, kind, allowed_hosts, default=False, forced=False):
    validate_url(url, allowed_hosts)
    name = " ".join(str(name or lang or "Subtitles").split()).replace('"', "'")[:80]
    try:
        lang = language(lang)
    except ValueError:
        lang = "und"
    key = hashlib.sha256(repr((url, name, lang, kind)).encode()).hexdigest()[:16]
    return {
        "id": key,
        "url": url,
        "name": name,
        "language": lang,
        "kind": kind,
        "default": bool(default),
        "forced": bool(forced),
    }


def roku_language(value):
    base = language(value).split("-")[0]
    return next(
        (key for key, code in LANGUAGES.items() if code == base),
        base if len(base) == 3 else "und",
    )


def choose(tracks, preferred):
    if not tracks:
        return None
    preferred = language(preferred)
    # Full subtitles in the preferred language beat forced-dialogue-only tracks.
    return min(
        tracks,
        key=lambda item: (
            item["language"].split("-")[0] != preferred.split("-")[0],
            item["forced"],
            item["language"] != preferred,
            not item["default"],
        ),
    )


async def discover(frame, allowed_hosts):
    result, seen = [], set()
    for frame in [frame] if frame is not None else []:
        try:
            rows = await frame.locator(
                'video track[kind="subtitles"], video track[kind="captions"]'
            ).evaluate_all(
                "els => els.slice(0, 32).map(e => ({url:e.src, name:e.label, language:e.srclang, default:e.default}))"
            )
        except Exception:
            continue
        for row in rows:
            try:
                item = track(
                    urljoin(frame.url, row["url"]),
                    row.get("name"),
                    row.get("language"),
                    "file",
                    allowed_hosts,
                    row.get("default", False),
                )
            except (ValueError, KeyError):
                continue
            if item["url"] not in seen:
                seen.add(item["url"])
                result.append(item)
                if len(result) == 32:
                    return result
    return result


TIMING = re.compile(
    r"(?P<start>(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+(?P<end>(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+[^\n]*)?"
)


def milliseconds(value):
    parts = value.replace(",", ".").split(":")
    hours, minutes, seconds = (0, *parts) if len(parts) == 2 else parts
    if int(minutes) >= 60 or float(seconds) >= 60:
        raise ValueError("invalid subtitle timestamp")
    return round((int(hours) * 3600 + int(minutes) * 60 + float(seconds)) * 1000)


def srt_time(value):
    seconds, millis = divmod(value, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def subtitle_text(data, *, segmented=False):
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("subtitle file exceeds 2 MiB")
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in text:
        raise ValueError("invalid subtitle text")
    vtt = text.startswith("WEBVTT")
    if vtt and not re.fullmatch(r"WEBVTT(?:[ \t].*)?", text.splitlines()[0]):
        raise ValueError("invalid WebVTT header")
    maps = [line for line in text.splitlines() if line.startswith("X-TIMESTAMP-MAP")]
    if maps:
        if len(maps) != 1 or not maps[0].startswith("X-TIMESTAMP-MAP="):
            raise ValueError("invalid WebVTT timestamp map")
        fields = maps[0].split("=", 1)[1].split(",")
        pairs = [field.split(":", 1) for field in fields]
        if len(pairs) != 2 or any(len(pair) != 2 for pair in pairs):
            raise ValueError("invalid WebVTT timestamp map")
        mapping = dict(pairs)
        if (
            set(mapping) != {"LOCAL", "MPEGTS"}
            or not re.fullmatch(r"(?:\d{2,}:)?\d{2}:\d{2}\.\d{3}", mapping["LOCAL"])
            or not mapping["MPEGTS"].isascii()
            or not mapping["MPEGTS"].isdigit()
        ):
            raise ValueError("invalid WebVTT timestamp map")
        milliseconds(mapping["LOCAL"])
        if not 0 <= int(mapping["MPEGTS"]) < 2**33:
            raise ValueError("invalid WebVTT MPEGTS timestamp")
    if segmented and not vtt:
        raise ValueError("HLS subtitles must be WebVTT")
    if not segmented and "X-TIMESTAMP-MAP" in text:
        raise ValueError("timestamp-mapped WebVTT must remain in its HLS presentation")
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        lines = block.splitlines()
        if not lines or (
            vtt and lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION"))
        ):
            continue
        index = 0 if "-->" in lines[0] else 1
        if index >= len(lines) or not (match := TIMING.fullmatch(lines[index])):
            raise ValueError("invalid subtitle cue")
        start, end = milliseconds(match["start"]), milliseconds(match["end"])
        if not 0 <= start < end <= 24 * 3600 * 1000:
            raise ValueError("invalid subtitle cue times")
        body = "\n".join(lines[index + 1 :])
        if not body or len(body) > 8192 or len(cues) >= 20000:
            raise ValueError("invalid subtitle cue size")
        cues.append(
            f"{len(cues) + 1}\n{srt_time(start)} --> {srt_time(end)}\n{re.sub(r'<[^>]*>', '', body)}\n"
        )
    if segmented:
        # Preserve cue times and X-TIMESTAMP-MAP, including empty segments.
        return text.encode(), "text/vtt"
    if not cues:
        raise ValueError("subtitle file has no cues")
    return ("\n".join(cues) + "\n").encode(), "application/x-subrip"
