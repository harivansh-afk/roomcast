"""Bounded, fail-closed media sampling; not a decode or visual playback proof."""

import asyncio
import json
import re
import tempfile
from fractions import Fraction
from pathlib import Path
from urllib.parse import urljoin

PROBE_TIMEOUT = 10
PREFLIGHT_TIMEOUT = 90
MAX_VARIANTS = 12
MAX_OUTPUT = 65536


def attributes(line):
    return dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(":", 1)[1]))


def value(attrs, key):
    return attrs.get(key, "").strip('"')


async def probe(data, executable):
    if not data or len(data) > 64 * 1024 * 1024:
        raise ValueError("invalid probe input size")
    with tempfile.TemporaryDirectory(prefix="roomcast-probe-") as directory:
        source = Path(directory) / "sample"
        source.write_bytes(data)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,mpegts,aac,ac3,eac3,mp3",
                "-probesize",
                "8388608",
                "-analyzeduration",
                "5000000",
                "-show_entries",
                "stream=codec_type,codec_name,profile,width,height,pix_fmt,level,avg_frame_rate,r_frame_rate,channels,sample_rate",
                "-of",
                "json",
                str(source),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise ValueError("media probe unavailable") from None
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                output = bytearray()
                while chunk := await process.stdout.read(4096):
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise ValueError("media probe output exceeded limit")
                await process.wait()
            if process.returncode:
                raise ValueError("media probe failed")
            result = json.loads(output)
            streams = result.get("streams")
            if not isinstance(streams, list) or not streams or len(streams) > 16:
                raise ValueError("media probe returned invalid streams")
            if not all(isinstance(s, dict) for s in streams):
                raise ValueError("media probe returned invalid streams")
            return streams
        except (TimeoutError, json.JSONDecodeError, AttributeError):
            raise ValueError("media probe timed out or returned invalid data") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


def audio_evidence(streams):
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        raise ValueError("presentation has no audio")
    result = []
    for stream in audio:
        try:
            channels = int(stream.get("channels", 0))
            rate = int(stream.get("sample_rate", 0))
        except (ValueError, TypeError):
            raise ValueError("unsupported audio parameters") from None
        codec = stream.get("codec_name")
        if (
            codec not in ("aac", "ac3", "eac3", "mp3")
            or not 1 <= channels <= 6
            or not 8000 <= rate <= 48000
        ):
            raise ValueError("unsupported audio parameters")
        if codec == "aac" and stream.get("profile") != "LC":
            raise ValueError("unsupported AAC profile")
        result.append({"codec": codec, "channels": channels, "sample_rate": rate})
    return result


def video_evidence(streams, max_height):
    video = [s for s in streams if s.get("codec_type") == "video"]
    if len(video) != 1:
        raise ValueError("presentation must contain one video stream")
    stream = video[0]
    try:
        width, height, level = (
            int(stream.get(k, 0)) for k in ("width", "height", "level")
        )
        fps = Fraction(stream.get("avg_frame_rate", "0/0"))
        if fps <= 0:
            fps = Fraction(stream.get("r_frame_rate", "0/0"))
    except (TypeError, ValueError, ZeroDivisionError):
        # MPEG-TS commonly supplies only r_frame_rate.
        try:
            fps = Fraction(stream.get("r_frame_rate", "0/0"))
            width, height, level = (
                int(stream.get(k, 0)) for k in ("width", "height", "level")
            )
        except (TypeError, ValueError, ZeroDivisionError):
            raise ValueError("unknown video parameters") from None
    if (
        stream.get("codec_name") != "h264"
        or stream.get("profile")
        not in ("Baseline", "Constrained Baseline", "Main", "High")
        or stream.get("pix_fmt") != "yuv420p"
    ):
        raise ValueError("video requires 8-bit 4:2:0 H.264")
    if not (
        0 < width <= 1920
        and 0 < height <= min(max_height, 1080)
        and 0 < level <= 42
        and 0 < fps <= 60
    ):
        raise ValueError("video exceeds FHD H.264 limits")
    # Level 4.2 maximum macroblocks/second, with lower levels capped at 30 fps
    # for FHD; avoids accepting implausible headers.
    if ((width + 15) // 16) * ((height + 15) // 16) * fps > (
        522240 if level == 42 else 245760
    ):
        raise ValueError("video exceeds H.264 frame-rate limits")
    return {
        "codec": "h264",
        "profile": stream["profile"],
        "pixel_format": "yuv420p",
        "width": width,
        "height": height,
        "level": level,
        "fps": round(float(fps), 3),
    }


async def decode_frame(data, executable):
    """Require one actual decoded frame from relayed bytes, never contact a URL."""
    with tempfile.TemporaryDirectory(prefix="roomcast-frame-") as directory:
        source, dest = Path(directory) / "sample", Path(directory) / "frame.raw"
        source.write_bytes(data)
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,mpegts,aac,ac3,eac3,mp3",
                "-threads",
                "1",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=64:64",
                "-pix_fmt",
                "gray",
                "-threads",
                "1",
                "-f",
                "rawvideo",
                "-fs",
                "4096",
                str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise ValueError("frame decoder unavailable") from None
        try:
            await asyncio.wait_for(process.wait(), PROBE_TIMEOUT)
            if process.returncode or not dest.exists() or dest.stat().st_size != 4096:
                raise ValueError("relayed video frame decode failed")
        except TimeoutError:
            raise ValueError("relayed video frame decode timed out") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def sample(session, url, *, video=False):
    key = session.register(url)
    body, final = await session.raw(url)
    if len(body) > 1024 * 1024:
        raise ValueError("playlist exceeds preflight limit")
    if b"#EXT-X-STREAM-INF:" in body:
        raise ValueError("nested HLS masters are not supported")
    _, segments = session.rewrite(body, final)
    if not segments:
        raise ValueError("playlist has no media segments")
    resource = session.resources[segments[0]]
    data, _ = await session.raw(resource.url)
    if resource.init:
        init, _ = await session.raw(resource.init)
        data = init + data
    streams = await probe(data, session.config.ffprobe)
    if video:
        video_evidence(streams, session.config.max_height)
    else:
        audio_evidence(streams)
    # Validate the exact repackaged bytes, not just the upstream headers.
    relayed, _ = await session.get(segments[0])
    if resource.init:
        streams = await probe(relayed, session.config.ffprobe)
    if video:
        video_evidence(streams, session.config.max_height)
        await decode_frame(relayed, session.config.ffmpeg)
    else:
        audio_evidence(streams)
    session.pin_playlist(url, body, final)
    return streams, key


async def prepare(session):
    session.preflight = {"state": "checking", "visual_verified": False}
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT):
            await _prepare(session)
    except asyncio.CancelledError:
        session.preflight = {"state": "cancelled", "visual_verified": False}
        raise
    except Exception:
        session.preflight = {
            "state": "failed",
            "visual_verified": False,
            "reason": "no complete compatible presentation verified",
        }
        raise ValueError(
            "media preflight failed: no complete compatible presentation verified"
        ) from None


async def _prepare(session):
    source = session.resources[session.root].url
    body, base = await session.raw(source)
    if len(body) > 1024 * 1024:
        raise ValueError("playlist exceeds preflight limit")
    lines = body.decode("utf-8-sig").splitlines()
    # Validate playlist restrictions before considering any rendition.
    variants = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = attributes(line)
            match = re.fullmatch(r"(\d+)x(\d+)", value(attrs, "RESOLUTION"))
            rank = (int(match[2]), int(match[1])) if match else (0, 0)
            if (
                index + 1 >= len(lines)
                or not lines[index + 1]
                or lines[index + 1].startswith("#")
            ):
                raise ValueError("invalid variant URI")
            variants.append((rank, index, attrs))
    if not variants:
        streams, _ = await sample(session, source, video=True)
        video, audio = (
            video_evidence(streams, session.config.max_height),
            audio_evidence(streams),
        )
        external = False
        attempted = 1
    else:
        if len(variants) > MAX_VARIANTS:
            raise ValueError("master variant limit exceeded")
        # A provisional choice allows syntax/URL validation, never playback.
        session.selections[base] = variants[0][1]
        session.rewrite(body, base)
        for attempted, (_, index, attrs) in enumerate(
            sorted(variants, key=lambda v: v[0], reverse=True), 1
        ):
            try:
                streams, _ = await sample(
                    session, urljoin(base, lines[index + 1]), video=True
                )
                video = video_evidence(streams, session.config.max_height)
                group = value(attrs, "AUDIO")
                renditions = [
                    (i, attributes(line))
                    for i, line in enumerate(lines)
                    if line.startswith("#EXT-X-MEDIA:")
                ]
                renditions = (
                    [
                        (i, a)
                        for i, a in renditions
                        if value(a, "TYPE") == "AUDIO" and value(a, "GROUP-ID") == group
                    ]
                    if group
                    else []
                )
                if group and not renditions:
                    raise ValueError("missing audio group")
                audio, external = [], False
                for _, rendition in renditions:
                    uri = value(rendition, "URI")
                    audio_streams = (
                        (await sample(session, urljoin(base, uri)))[0]
                        if uri
                        else streams
                    )
                    audio.extend(audio_evidence(audio_streams))
                    external = external or bool(uri)
                if not renditions:
                    audio = audio_evidence(streams)
                session.selections[base] = index
                break
            except Exception:
                continue
        else:
            raise ValueError("no compatible master variant")
    session.preflight = {
        "state": "compatible",
        "method": "ffprobe_and_first_frame",
        "local_frame_decoded": True,
        "video": video,
        "audio": audio,
        "external_audio": external,
        "variants_attempted": attempted,
        "visual_verified": False,
    }
    session.pin_playlist(source, body, base)
    await session.get(session.root)
