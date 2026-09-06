# Timestamp playback and seeking

The service accepts an absolute `start_seconds` on `/play`, and `/seek` accepts
`{"seconds":1200,"mode":"absolute"}` for 20:00. Omitting `mode` keeps the existing
relative-seconds API. Absolute timestamps are integers from 0 to 21600; relative
jumps are nonzero integers within one hour. A relative rewind past the beginning
lands at zero. Unknown duration and timestamps at or past the end are rejected.
`roomcast pause` and `roomcast resume` remain the controls for stopping and
continuing the clock without ending the episode.

## Why a player change is required

The relay already serves the full VOD timeline, retaining the audio/video
initialization maps and timestamps. The current limitation is the controller:
Roomcast previously allowed only relative YouTube seeks, while
[Media Assistant 1.3.1](https://github.com/MedievalApple/Media-Assistant/tree/1335dd41aad175ad7494f01f7e2c21a582d06179)
does not handle a start timestamp or seek action. Its `timeOffset` changes the
audio time display; it is not a video start offset.

The optional player package applies a small patch to that pinned upstream source:

- Launch/input `startSeconds` sets `ContentNode.PlayStart` before playback.
- Input `a=seek&positionSeconds=1200&stayPaused=true|false` sets `Video.seek`.
- `seekMode="accurate"` requests precise decoder seeking; `autoplayAfterSeek`
  preserves a paused seek. Normal autoplay is restored on resume/new playback.
- Native `enableTrickPlay` stays enabled. The full episode playlist stays intact.

These are documented Roku [content fields](https://developer.roku.com/dev/docs/content-metadata)
and [Video controls](https://developer.roku.com/dev/docs/video). Accurate seeking
depends on the device decoder; a successful HTTP command alone is not evidence
of an accurate landing. No voice-transport capability is advertised by the patch.

## Optional installation, after viewing

Building does not install the app or contact the TV:

```sh
nix build .#roku-player
```

The output is `result/roomcast-player.zip`. It contains the pinned Media Assistant
source and assets with the timestamp patch; upstream's Apache-2.0 license is
preserved, and the Roomcast changes are covered by `ROOMCAST-LICENSE` (GPL-3.0).
The app title is Roomcast Player. No upstream artwork is checked into this repo.

When ready to test, sideload the ZIP using Roku's development installer. Sideloading
uses the `dev` app slot and replaces any existing development app. Then configure:

```nix
services.roomcast = {
  playerAppId = "dev";
  playerSupportsSeeking = true;
};
```

The equivalent JSON fields are `roku_app_id: "dev"` and
`roku_seek_enabled: true`. Deploy the service config only after the app is
installed. The defaults remain stock Media Assistant (`782875`) with timestamp
commands disabled; enabling the flag with that stock app ID is rejected.
YouTube uses its existing paired control connection instead of this player.
A YouTube timestamp start first launches and verifies the native app, then seeks;
it can briefly show the beginning or an ad before the seek completes.

## Confirmation and remaining acceptance

Startup still requires the correct app/player, both media tracks, delivery from
the new relay session, and sustained progress. A nonzero start also checks the
first observed playing position against the requested offset, allowing elapsed
playback time since launch. Status includes the requested and observed position.

A seek returns `confirmed`, `target_seconds`, `actual_seconds`, `tolerance_seconds`
and `roku`. Confirmation requires a first healthy sample within two seconds of
the target, then a second sample that progresses normally or remains paused.
`actual_seconds` is the second sample, so playing video may have advanced beyond
the initial landing tolerance. Ordinary progression from the old position is
excluded for jumps larger than that tolerance. Missing audio/video, an app change,
a wrong landing, an unexpected resume or a timeout cannot report success.
This verifies reported device state, not the contents of a rendered frame.

During a seek the job reports `seeking`; the playback monitor resumes afterwards.
Failed seeks retain `job.seek_error` while monitoring the current playback.
Stop/home cancel pending confirmation; another play or seek request cannot race it.
The seek operation has a 65-second bound, including paired YouTube connection and
up to 30 seconds of position confirmation. Provider URLs can still expire, and a
seek near the end can finish before enough confirmation samples arrive.

Offline checks cover service requests, CLI translation, Roku command parameters,
position confirmation, pause/cancellation, media decoding and the player build.
BrightScript compilation can be repeated against the patched source with
`npm exec --yes --package=brighterscript@0.73.1 -- bsc --rootDir PATH --outFile /tmp/roomcast-player.zip`.

Physical acceptance is pending. On the target TV, check a non-keyframe 20-minute
start, forward/backward seeks, seeking to zero, a paused seek followed by resume,
native remote scrubbing before the start offset, and audio/video continuity on
both split fMP4 and muxed HLS. This change has not been installed or tested on the
TV; no live playback was interrupted for its development.
