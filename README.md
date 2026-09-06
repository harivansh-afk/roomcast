# Roomcast

Text an agent to play a movie or episode on a Roku. Roomcast finds a stream in an
isolated browser, serves it over the LAN, and opens Media Assistant on the TV.
It copies compressed video/audio rather than recording the desktop or encoding
new video. The Roku app need not already be open.

The supported site adapter is Cinejoy. Every play request discovers its current
server menu and resolves fresh stream URLs. Failed candidates advance to the next
server; no provider name or CDN hostname is pinned. Separate H.264/AAC fMP4 tracks keep their original initialization data and
timestamps. Combined audio/video fMP4 is repackaged into MPEG-TS HLS on demand. This is not a universal website
player: site layout changes, unsupported codecs, encrypted streams and expired
URLs can fail. No DRM handling or full video transcoding is implemented.

## Use

Install Media Assistant (Roku app 782875), enable Control by mobile apps and Fast
TV Start. Pair the TV by serial number. Addresses are discovered at runtime; DHCP
reservations are not required.

```sh
roomcast search 'The Gentlemen'
roomcast search 'Daft Punk official audio' --source youtube
roomcast play youtube aqz-KE-bpKQ
roomcast sources
roomcast play tv 236235 --season 1 --episode 1
roomcast status
roomcast pause
roomcast resume
roomcast stop
```

Play is asynchronous. A `queued` response means accepted, not playing. Poll status
until `job.state` is `playing` or `failed`; `roku.state` is the current playback
state. Verification requires the intended app and player, both audio and video
formats, progressing playback and delivery from the new session. Playback is
monitored after startup; paused, buffering, ended and failed states remain visible. Starting a new title while something is playing
requires `--replace`.

To start at a timestamp or scrub by text, install the optional
[Roomcast Roku player](docs/timestamp-seeking.md) and enable its capability in
the service config. Stock Media Assistant supports its native remote seek UI,
but does not accept Roomcast timestamp commands.

```sh
roomcast play tv 236235 --season 1 --episode 1 --start 20:00
roomcast seek --to 20:00
roomcast seek -30
```

The MCP equivalents are `play(..., start_seconds=1200)` and
`seek(seconds=1200, mode="absolute")`. A failed or unconfirmed seek reports an
error; paused playback must remain paused. `stop` also cancels a pending seek.

With [subtitle control enabled](docs/subtitles.md), each new episode requests
available subtitles on, preferring English. You can list tracks, change language,
or turn captions off for the current episode:

```sh
roomcast subtitles
roomcast subtitles off
roomcast subtitles on
roomcast subtitles --language hi
```

The MCP tool is `subtitles(enabled=True, language="en")`; call it without arguments
to list tracks and reported player state. Install Roomcast Player 1.3.4 for the
provider subtitle metadata fix.
Sources without supported subtitles are reported as unavailable.

An agent can use `roomcast-mcp` over stdio instead of shell access. It exposes
search, playback, status, controls, seeking, source discovery and isolated browsing; it does not accept arbitrary source URLs,
file paths or shell commands. Configure the client with:

```json
{"mcpServers":{"roomcast":{"command":"roomcast-mcp"}}}
```

## NixOS

Import `roomcast.nixosModules.default` and configure:

```nix
services.roomcast = {
  enable = true;
  lanInterface = "wlan0";
  rokuSerial = "YOUR_TV_SERIAL";
  rokuAddress = "10.0.0.20";
  discoveryNetworks = [ "10.0.0.0/24" ];
};
```

A systemd media-only socket follows `lanInterface` at `port` (default 18795).
Control stays on `/run/roomcast/control.sock` (group `roomcast`, mode 0660).
Media requests require both a session token and the discovered TV source address.
The firewall exposes TCP `port` and UDP `discoveryPort` only on that interface.
There is no proxy process or second media port in the NixOS deployment.

The TV's serial is authoritative; `rokuAddress` is an optional starting hint.
Discovery tries cached addresses, the optional `rokuMac` neighbor entry, SSDP,
then `discoveryNetworks`. These explicit private CIDRs are limited to 1024 total
addresses and queried in batches of 16, only on TCP 8060. Spark's advertised IP
comes from the kernel route to the verified TV. No network settings are modified
by the agent. Recovery is checked before playback/control; address changes during
an existing stream can still require a new play request. Networks that block all
peer traffic cannot be repaired by discovery.

Use `config.services.roomcast.package` for client binaries so package overrides
apply to both service and clients. Add only intended clients to the roomcast group.

## YouTube and source discovery

Use `search --source youtube` for videos or music and pass the returned video ID
to `play youtube`. Playback launches the native YouTube app without relaying its
video. Precise seeking uses a paired YouTube TV connection: open YouTube Settings,
choose Link with TV code, and run `roomcast pair-youtube`. The CLI reads the code
without echoing it. Tokens remain in private mutable service state, outside Git
and the Nix store. `roomcast seek 30` requests a relative jump and verifies the TV
position; an unconfirmed seek fails rather than reporting success. Pairing and
precise seeking still require live acceptance on the target TV.

`sources` reads `directoryUrl` (BestFreeStreaming by default), caches its current
list for an hour, and returns opaque source IDs. Pass a returned ID as `--source`
to search/play a site with a compatible layout. Directory membership is not proof
of adapter compatibility; unfamiliar layouts still require an adapter update.
When a site's layout differs, the MCP `browse` tool opens its directory entry in
an isolated browser. The agent can inspect numbered controls, click/type/press
Enter, and choose an opaque captured stream ID with `play(kind="browser", id=...)`.
The browser exposes neither a shell nor caller-supplied JavaScript, uses a fresh
profile, and expires interactive sessions after ten minutes. Only public HTTPS
HLS captures are eligible for playback; request cookies/authorization headers
are not forwarded. Sites requiring those cookies or unsupported media may fail.
The directory and page contents are data, never instructions or code. Captchas,
layout quirks and incompatible streams can still prevent playback.

The service runs as its own user with no access to home directories, a private
temporary directory, a 2 GiB memory ceiling, and bounded process/CPU use. Chromium
uses its sandbox and a fresh context for each lookup. There is no personal browser
profile, saved login, KB access, SSH key or agent memory in the service.

## Streaming behavior

Master playlists select at most the configured resolution (1080p by default).
The relay accepts only HTTPS media URLs and rejects non-public DNS answers before
connecting, including on redirects. `allowedMediaHosts` can optionally restrict
these further to an exact hostname list; its default `null` permits the current
public CDNs discovered by the site adapter. No caller can submit arbitrary URLs.
`siteUrl` changes the origin for a compatible Cinejoy site after a domain migration;
it does not make the adapter understand another website's layout. It rewrites nested playlists to opaque, session-scoped
URLs and handles required provider request headers server-side. Separate audio/video fMP4 keeps HLS initialization maps and original fragments.
Combined fMP4 uses FFmpeg stream copy to MPEG-TS. Compressed media is not
re-encoded for playback; complete segments are decoded locally for validation
before being served. Provider codec hints are omitted and measured dimensions
are published. See [playback verification](docs/video-preflight.md).

Source resolution is lazy: the browser yields a server's current candidates,
playback prepares and verifies them, and only failures advance to another server.
At most eight menu entries and three candidate URLs per server are tried. Browser
contexts close on success, failure or cancellation; no resolved URL is persisted
across play requests. Site changes still require adapter maintenance. Expiry during
an already-playing session currently requires a new play request; seamless URL
renewal and resume are not implemented.

There is one active playback session. Responses share an LRU cache capped at
128 MiB; four upstream jobs and two remux processes may run at once. Each media
object is capped at 32 MiB. Remux processes have a ten-second deadline, and
complete-segment validation has a twenty-second deadline. A session
expires after six hours. Stop invalidates its token and cancels its work. The
first two segments of a requested media playlist are prefetched. Further segments
are fetched when Roku asks, so the entire episode is not downloaded upfront.

The full VOD playlist remains available for Roku's native seek UI, including
positions before a requested start timestamp. Recovery after upstream URL expiry
and persistent resume positions are not implemented yet.
Pause/resume and stop are supported.
A service restart ends the relay session; request playback again.

## Browser isolation

Browser request checks are defense in depth, not a complete network sandbox.
Before allowing untrusted clients, enforce browser egress restrictions at the
OS/network layer. Agent identities, permissions and messaging integrations belong
to the caller, outside Roomcast.

## Development and verification

```sh
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run python -m unittest discover -s tests -v
nix build
```

Set `SSL_CERT_FILE` to the system CA bundle when a standalone Python installation
cannot find it. A local config JSON supplies `roku_ip`, `roku_serial`, `public_base`,
`chromium`, and `ffmpeg`; run `roomcast-service --config /path/to/config.json`.
Use `ROOMCAST_SOCKET` to select a development control socket. Never commit provider
stream URLs or browser state. [Live acceptance evidence](docs/acceptance.md).
