# Playback verification

A progressing Roku clock does not establish working picture and sound. Roomcast
validates media before publishing it and checks the device during playback.

## Presentation selection and delivery

Select a compatible H.264 8-bit 4:2:0 presentation within the configured FHD limit,
with supported audio. Actual decoded dimensions, profile and level decide
compatibility. Publish measured dimensions and frame rate, and omit untrusted
CODECS hints: a provider was advertising AVC level 5.0 for actual level 4.0,
causing Roku to reject the master before requesting media.

When a master references separate audio, select one usable default/autoselect
track from its associated group. An unusable optional language does not reject
an otherwise complete presentation. Require matching playlist durations and
initial audio/video timestamps. Preserve the original fMP4 initialization maps,
fragments and timestamps for separate tracks. Combined audio/video fMP4 continues
to use the tested MPEG-TS stream-copy path. Do not convert standalone audio into
an audio-only transport stream.

Before launch, decode one complete segment at the requested start position of each
selected track, with audio and video prepared concurrently. Prefer variant hints
within the configured height before trying oversized hints; actual decoded media
still decides compatibility. A small fMP4 init probe rejects known incompatible
dimensions/codecs before fetching the large fragment. Init probes can omit profile,
pixel format and frame rate; missing metadata is deferred to the actual media
probe rather than treated as incompatibility. Verify decoded audio/video, codec constraints and
segment durations. Compare audio/video timestamp origins after accounting for
their independently segmented playlist offsets. Every subsequently fetched media segment undergoes the same validation
before it can be served, including after cache eviction. Black frames and silence
can be intentional content and are not treated as corruption. Distant samples do
not prove the whole movie valid, so they no longer block startup. Corrupt later
segments fail when fetched for playback or a seek.

Pin selected playlists and initialization bytes within a shared 2 MiB budget.
Segment responses remain in the bounded 128 MiB LRU. Four jobs and at most two
remux processes run concurrently. At most two speculative jobs run alongside
demand requests. A small rolling window follows delivery and seeks; it never
recursively downloads ahead. Identical initialization downloads are coalesced,
and unchanged media bytes occupy one response-cache entry. FFmpeg reads local files only with explicit
protocol/demuxer allowlists; its complete-segment decode has a 20-second deadline
and a 1 MiB hash-output limit. Segments must be no longer than 30 seconds. The
presentation preflight has a 90-second ceiling, and the whole startup request has
a 110-second ceiling, including resolution and Roku confirmation.

Transient upstream HTTP/network errors retry up to three times within 70 seconds.
Policy errors and permanent HTTP failures do not retry. Failed candidates advance
to another source/provider during startup. Cancellation kills media subprocesses
and invalidates published candidate sessions.

## Device verification

`playing` requires the intended Roku app and player, both reported audio/video
formats, two consecutive advancing position samples, and delivery of both tracks
from the new relay session. Native YouTube requires the same device checks; its
media delivery is owned by the YouTube app.

Startup polls every 250 ms and concurrently reads the app and media-player state
after verifying device identity. Position samples must belong to a healthy
interval with delivery from the current session; progress before that interval
does not count. Faster confirmation does not imply faster physical rendering.

After startup, poll every three seconds. Reflect pauses, buffering and app changes;
report a failure for a missing track lasting nine seconds, playback stalled for
30 seconds, Roku errors or failed segment delivery. Ended/failed sessions close
and release their resources. Runtime failure does not silently restart the title
or claim success; seamless provider replacement at the current position is not
implemented.

`preflight` contains local media evidence. `roku` contains device observations.
`visual_verified=false` remains explicit because ECP cannot see the television or
hear its speakers. Physical confirmation is recorded in acceptance notes.

## Boundaries

Unsupported codecs, encryption, byte ranges, live/nested HLS, corrupt upstream
content and expired URLs can still fail. There is no video transcoder or DRM
handling. A source can serve wrong content that is technically valid. Device
telemetry cannot prove picture quality, audible speaker output or full-episode
playback; these are not promised by passing startup checks.

Regression tests generate real fMP4 and MPEG-TS fixtures and cover separate and
combined tracks, missing audio/video, bad later segments, changed cached media,
initialization pinning, false codec metadata, source/track fallback, retries,
cancellation, missing device tracks, stalled playback and pause/resume state.
