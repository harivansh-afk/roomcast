"""Verify the selected presentation and the exact media bytes sent to the TV."""

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


async def decode_media(data, executable, required):
    """Decode a complete segment, counting video frames and audio samples.

    Frame hashes keep subprocess output bounded without retaining decoded media.
    Black frames and silence are valid content, so neither is a rejection rule.
    """
    with tempfile.TemporaryDirectory(prefix="roomcast-decode-") as directory:
        source = Path(directory) / "sample"
        source.write_bytes(data)
        args = [
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
            "-copyts",
            "-i",
            str(source),
        ]
        for kind in required:
            args += ["-map", "0:" + kind[0] + ":0"]
        if "video" in required:
            args += ["-vf", "scale=64:64", "-pix_fmt", "yuv420p"]
        if "audio" in required:
            args += ["-ac", "2", "-ar", "48000"]
        args += ["-threads", "1", "-filter_threads", "1", "-f", "framehash", "-"]
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        except OSError:
            raise ValueError("media decoder unavailable") from None
        try:
            async with asyncio.timeout(20):
                output = bytearray()
                while chunk := await process.stdout.read(8192):
                    output.extend(chunk)
                    if len(output) > 1024 * 1024:
                        raise ValueError("decoded segment exceeds limit")
                await process.wait()
            if process.returncode:
                raise ValueError("media segment decode failed")
            tracks, timebases = {}, {}
            for line in output.decode().splitlines():
                if line.startswith("#tb "):
                    index, scale = line[4:].split(": ", 1)
                    timebases[int(index)] = Fraction(scale)
                elif line and not line.startswith("#"):
                    fields = [field.strip() for field in line.split(",")]
                    index, pts, duration, size = (int(fields[i]) for i in (0, 2, 3, 4))
                    if size <= 0:
                        continue
                    start = float(pts * timebases[index])
                    end = float((pts + duration) * timebases[index])
                    track = tracks.setdefault(
                        index, {"frames": 0, "start": start, "end": end}
                    )
                    track["frames"] += 1
                    track["end"] = max(track["end"], end)
            if set(tracks) != set(range(len(required))):
                raise ValueError("media decode is missing a required track")
            return {kind: tracks[i] for i, kind in enumerate(required)}
        except TimeoutError:
            raise ValueError("media segment decode timed out") from None
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def inspect_media(data, config, role, duration):
    streams = await probe(data, config.ffprobe)
    required = ("video", "audio") if role == "muxed" else (role,)
    evidence = {}
    if "video" in required:
        evidence["video"] = video_evidence(streams, config.max_height)
    if "audio" in required:
        evidence["audio"] = audio_evidence(streams)
    decoded = await decode_media(data, config.ffmpeg, required)
    for kind, track in decoded.items():
        span = track["end"] - track["start"]
        if span < duration - 0.35 or span > duration + 1:
            raise ValueError(f"{kind} duration does not match its HLS segment")
    if (
        role == "muxed"
        and abs(decoded["video"]["start"] - decoded["audio"]["start"]) > 0.5
    ):
        raise ValueError("audio and video timestamps do not align")
    return {**evidence, "decoded": decoded}


async def sample(session, url, role):
    body, final = await session.raw(url)
    if len(body) > 1024 * 1024 or b"#EXT-X-STREAM-INF:" in body:
        raise ValueError("invalid or nested media playlist")
    session.roles[final] = role
    _, segments = session.rewrite(body, final)
    if not segments:
        raise ValueError("playlist has no media segments")
    session.pin_playlist(url, body, final)
    # Consecutive startup segments plus distant samples catch late track changes.
    indices = sorted(
        {0, min(1, len(segments) - 1), len(segments) // 2, len(segments) - 1}
    )
    for index in indices:
        await session.get(segments[index])
    evidence = session.evidence[segments[0]]
    return (
        evidence,
        sum(session.resources[key].duration for key in segments),
        len(indices),
    )


async def prepare(session):
    session.preflight = {"state": "checking", "visual_verified": False}
    try:
        async with asyncio.timeout(PREFLIGHT_TIMEOUT):
            await _prepare(session)
    except asyncio.CancelledError:
        session.preflight = {"state": "cancelled", "visual_verified": False}
        raise
    except Exception as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        session.preflight = {
            "state": "failed",
            "visual_verified": False,
            "reason": reason[:400],
        }
        raise ValueError("media preflight failed: " + reason[:400]) from None


async def _prepare(session):
    source = session.resources[session.root].url
    body, base = await session.raw(source)
    if len(body) > 1024 * 1024:
        raise ValueError("playlist exceeds preflight limit")
    lines = body.decode("utf-8-sig").splitlines()
    variants = []
    renditions = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-MEDIA:"):
            renditions.append((index, attributes(line)))
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
        evidence, duration, count = await sample(session, source, "muxed")
        audio, video = evidence["audio"], evidence["video"]
        external, attempted = False, 1
    else:
        if len(variants) > MAX_VARIANTS:
            raise ValueError("master variant limit exceeded")
        failures = []
        for attempted, (_, index, attrs) in enumerate(
            sorted(variants, reverse=True), 1
        ):
            group = value(attrs, "AUDIO")
            tracks = (
                [
                    (i, a)
                    for i, a in renditions
                    if value(a, "TYPE") == "AUDIO" and value(a, "GROUP-ID") == group
                ]
                if group
                else []
            )
            if group and not tracks:
                failures.append("missing audio group")
                continue
            # Select one usable default/autoselect track; unrelated languages must
            # neither break playback nor reach the TV without verification.
            tracks.sort(
                key=lambda pair: (
                    value(pair[1], "DEFAULT") != "YES",
                    value(pair[1], "AUTOSELECT") != "YES",
                )
            )
            for audio_index, rendition in tracks or [(None, {})]:
                try:
                    uri = value(rendition, "URI")
                    session.selections[base] = index
                    session.audio_selections[base] = audio_index
                    session.rewrite(body, base)
                    external = bool(uri)
                    evidence, duration, count = await sample(
                        session,
                        urljoin(base, lines[index + 1]),
                        "video" if external else "muxed",
                    )
                    video = evidence["video"]
                    if external:
                        sound, audio_duration, audio_count = await sample(
                            session, urljoin(base, uri), "audio"
                        )
                        if abs(duration - audio_duration) > 1:
                            raise ValueError(
                                "audio and video playlists have different durations"
                            )
                        if (
                            abs(
                                evidence["decoded"]["video"]["start"]
                                - sound["decoded"]["audio"]["start"]
                            )
                            > 0.5
                        ):
                            raise ValueError(
                                "external audio and video timestamps do not align"
                            )
                        audio = sound["audio"]
                        count += audio_count
                    else:
                        audio = evidence["audio"]
                    break
                except Exception as error:
                    failures.append(
                        str(error)
                        if isinstance(error, ValueError)
                        else type(error).__name__
                    )
            else:
                continue
            break
        else:
            raise ValueError(
                "no complete compatible variant: "
                + "; ".join(dict.fromkeys(failures))[:300]
            )
    session.preflight = {
        "state": "compatible",
        "method": "segment_audio_video_decode",
        "video": video,
        "audio": audio,
        "external_audio": external,
        "segments_sampled": count,
        "duration_seconds": round(duration, 3),
        "variants_attempted": attempted,
        "visual_verified": False,
    }
    if variants:
        # Provider CODECS/RESOLUTION tags can contradict the decoded media (for
        # example advertising AVC level 5.0 for actual level 4.0). Roku may reject
        # those headers before fetching a single segment. CODECS is optional;
        # let the demuxer identify it and publish the measured dimensions.
        attrs = attributes(lines[index])
        attrs.pop("CODECS", None)
        attrs.pop("DEFAULT", None)
        attrs["RESOLUTION"] = f"{video['width']}x{video['height']}"
        attrs["FRAME-RATE"] = str(video["fps"])
        lines[index] = "#EXT-X-STREAM-INF:" + ",".join(
            f"{k}={v}" for k, v in attrs.items()
        )
        body = ("\n".join(lines) + "\n").encode()
    session.pin_playlist(source, body, base)
    await session.get(session.root)
