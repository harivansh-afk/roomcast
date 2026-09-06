# Compatible-video preflight

Roomcast now samples actual media before launching non-YouTube playback. A
successful fetch or advancing Roku clock alone is not evidence of visible video.

## Supported path

- Require one H.264 Baseline/Main/High video stream, 8-bit 4:2:0, actual dimensions
  no larger than 1920×1080 and the configured height cap, level no higher than
  4.2, and bounded frame rate. This is a conservative FHD policy, not automatic
  capability discovery for every Roku model.
- Require supported audio (AAC-LC, AC3, EAC3 or MP3 with bounded channels/rate),
  either multiplexed with video or in the master's associated audio group.
- Probe representative upstream and remuxed fragments with ffprobe and locally
  decode one relayed video frame. Audio-only and incomplete video-only playlist
  candidates are rejected.
- Try master variants in descending advertised resolution, validating actual
  dimensions and encoding. A falsely advertised 1080p variant cannot bypass the
  probe. Keep the selected video and its verified external audio group together.
- Pin validated master and child playlist bytes outside the segment LRU, bounded
  to 2 MiB per session. Eviction cannot refetch a reordered master and silently
  switch to an unverified rendition. Stop clears pins and cancels preparation.
- Keep `preflight` evidence separate from Roku status: `local_frame_decoded=true`
  is local decoding only; `visual_verified=false` remains explicit.

Probes use local files only with protocol/demuxer allowlists, bounded subprocess
runtime/output, bounded playlist sizes and variant counts, and a 90-second
per-candidate preflight ceiling. Existing URL validation, DNS/redirect guards,
media authorization and explicit playback replacement rules remain in place.

## Verification

- 47 tests pass, including generated FFmpeg video/audio fixtures and regression
  checks for oversized actual video, incompatible profiles/pixel formats,
  audio-only and video-only rejection, decode failure, external audio retention,
  lower-rendition fallback, pinned playlist eviction and cancellation.
- The Nix package builds with ffprobe supplied by the module and FFmpeg tools
  available during checks. Ruff lint/format and whitespace checks pass.
- A fresh read-only Silo S01E05 Lisbon lookup rejected higher variants and chose
  actual 1440×720 H.264 Main level 3.2, retaining four AAC-LC stereo tracks. Local
  frame decode succeeded. The chosen master remained stable after cache eviction;
  the standalone video and audio candidates were rejected. No TV was launched.

## Limits and acceptance still required

This is not a promise of 100% playback. Source outages, signed URL expiration,
network changes, corrupt later segments, changing encodings, device-specific
quirks and DRM remain possible. It samples the first segment, not the whole
movie. A local decoded frame does not prove the television displays a picture.
No new transcoding path is included: unsupported sources fail clearly or advance
to another candidate/provider. All advertised tracks in the chosen audio group
must validate; a bad optional language track can cause conservative rejection.
Nested masters, encrypted/byte-range streams and incomplete presentations remain
unsupported. Preserving pinned VOD playlists does not renew expired media URLs.

Before deployment is called complete, authorize a Nix pin bump, deploy it, then
verify the actual TV displays the correct episode with audible sound, pause/resume
and sustained playback. Do not merge/deploy merely because a player clock moves.
