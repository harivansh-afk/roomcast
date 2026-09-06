# Subtitle controls

Every new Roomcast movie or episode requests subtitles on by default. English is
preferred; full subtitles take priority over forced-dialogue-only tracks in the
same language. If English is absent, an available track is selected. A source with
no supported tracks reports that fact and continues video/audio playback.

## Enable after installing the player

Build `nix build .#roku-player` and install `result/roomcast-player.zip` using the
Roku development installer when viewing has finished. This version is Roomcast
Player 1.3.3. Sideloading replaces the existing `dev` app. Configure the service:

```nix
services.roomcast = {
  playerAppId = "dev";
  playerSupportsSeeking = true;
  playerSupportsSubtitles = true;
  subtitlesOnByDefault = true;
  subtitleLanguage = "en";
};
```

The last two values are the defaults. JSON config uses `roku_subtitle_control`,
`subtitles_enabled`, and `subtitle_language`. Keep `playerSupportsSubtitles`
disabled for stock Media Assistant or the older 1.3.2 player; neither implements
this control protocol. No TV settings or service configuration are changed by
building or merging this package.

## Use

```sh
roomcast subtitles
roomcast subtitles off
roomcast subtitles on
roomcast subtitles --language hi
roomcast subtitles --track TRACK_ID
```

Use a track ID returned by the first command. An explicit language or track
selection that is unavailable fails before sending any TV command. On/off and
language changes affect the current episode; the next play request returns to
the configured defaults. The Roku remote's caption menu remains usable.

MCP exposes `subtitles()` for reading state and
`subtitles(enabled=True, language="en")` or `subtitles(enabled=False)` for changes.
The HTTP equivalents are `GET /subtitles` and `POST /subtitles` with `enabled`,
`language`, or `track_id`. Control stays on the existing Unix socket. Stop/home
cancel a pending subtitle change. Caption selection does not restart the video.

Roku exposes caption mode as a global setting: changing captions on/off also
changes its system caption mode. The player uses the documented
[`globalCaptionMode`, `subtitleTrack`, and `currentSubtitleTrack` fields](https://developer.roku.com/dev/docs/video).
The service reports the requested preference separately from the player's
reported caption mode and whether that request is applied. After using the remote,
the reported state can differ from the original request.

## Supported sources and timing

- HLS subtitle renditions in the selected variant's `SUBTITLES` group are retained
  and relayed, including language/name metadata. WebVTT segments retain their cue
  times and `X-TIMESTAMP-MAP`, so the native player can synchronize them after seeks.
- Provider HTML `<video><track>` files are discovered in the same frame that
  requested the candidate video. This avoids collecting subtitles from other
  provider frames or advertisements. Captured-browser playback uses the same rule.
- UTF-8 SRT and standalone WebVTT files are validated and served as SRT with the
  full episode timestamps and a `.srt` URL. The selected file is checked before
  asking the player to use it. Text formatting tags are removed during conversion.
  Files with HLS timestamp maps are kept in their HLS presentation rather than
  converted into standalone files.

The player receives sidecar metadata through Roku's
[`SubtitleTracks` content field](https://developer.roku.com/dev/docs/content-metadata).
No subtitle offset is subtracted when starting at 20 minutes or seeking backwards.
Playback retains the full video timeline and the full subtitle timeline.

Discovery is bounded to 32 HLS subtitle entries and eight provider files per
candidate. Subtitle objects are capped at 2 MiB; cue sizes/counts and timestamp
maps are checked. The existing public-HTTPS/DNS rules, session tokens, TV address
restriction and cache budget also cover subtitles. Failed subtitle delivery is
exposed as `subtitles.error` and does not mark the audio/video relay as failed.

This does not download subtitles from unrelated sites, generate captions, or
implement a subscription subtitle service. Provider tracks hidden behind custom
JavaScript APIs, authentication, or unsupported formats may be unavailable.
TTML/DFXP, image subtitles, fMP4 subtitle renditions, and explicit embedded
CEA-608/708 track selection are not implemented. Native YouTube caption controls
remain outside this API. A successful title can still lack usable subtitles.

## Confirmation and testing

ECP's [media-player status](https://developer.roku.com/dev/docs/external-control-api)
exposes a caption format, but not enough information to prove the selected language.
The player therefore reports native caption mode and track selection back to
`POST /subtitle-state/{session-token}` on the existing media listener. Only the
current TV address, session, command ID and matching track/preference are accepted.
Per-command sequence numbers reject delayed reports from older settings.

`confirmed: true` means the player reported the requested track and caption mode.
It does not prove that glyphs were visible at a particular frame or that a provider
matched the dialogue correctly. Missing acknowledgement fails after eight seconds;
the whole operation, including a sidecar fetch, is bounded to twenty seconds.
`status` also includes `roku.caption_format` and the current subtitle request,
available tracks, player report, and any delivery error.

Offline tests cover discovery, language/default selection, full-timeline SRT
conversion, HLS timestamp maps, private URL rejection, malformed captions,
stale/foreign acknowledgements, missing acknowledgement, cancellation and
caption errors that leave audio/video running. The player is compiled with
BrighterScript 0.73.1 and packaged through the Nix check.

Live acceptance is pending. Verify visible English captions on a fresh episode,
on/off and language changes during play and pause, the remote caption menu,
caption synchronization after a 20-minute start and forward/backward seeks, and
clear missing-caption behavior. No live TV commands or installation were used
to develop this change.
