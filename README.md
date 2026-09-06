# Roomcast

Text an agent to play a movie or episode on a Roku. Roomcast finds a stream in an
isolated browser, serves it over the LAN, and opens Media Assistant on the TV.
It copies compressed video/audio rather than recording the desktop or encoding
new video. The Roku app need not already be open.

The supported site adapter is Cinejoy. Every play request discovers its current
server menu and resolves fresh stream URLs. Failed candidates advance to the next
server; no provider name or CDN hostname is pinned. Compatible H.264/AAC fragmented
MP4 is repackaged into MPEG-TS HLS on demand. This is not a universal website
player: site layout changes, unsupported codecs, encrypted streams and expired
URLs can fail. No DRM handling or full video transcoding is implemented.

## Use

Install Media Assistant (Roku app 782875), enable Control by mobile apps and Fast
TV Start. Reserve the TV and relay addresses in DHCP. The service checks the TV's
serial before every action; an address reassigned to another TV fails closed.

```sh
roomcast search 'The Gentlemen'
roomcast play tv 236235 --season 1 --episode 1
roomcast status
roomcast pause
roomcast resume
roomcast stop
```

Play is asynchronous. A `queued` response means accepted, not playing. Poll status
until `job.state` is `playing` or `failed`; `roku.state` is the current playback
state. Verification requires Media Assistant to be active and its position to
advance without a reported error. Starting a new title while something is playing
requires `--replace`.

An agent can use `roomcast-mcp` over stdio instead of shell access. It exposes only
`search`, `play`, `status` and `control`; it does not accept arbitrary source URLs,
file paths or shell commands. Configure the client with:

```json
{"mcpServers":{"roomcast":{"command":"roomcast-mcp"}}}
```

## NixOS

Import `roomcast.nixosModules.default` and configure:

```nix
services.roomcast = {
  enable = true;
  lanAddress = "10.0.0.10";
  rokuAddress = "10.0.0.20";
  rokuSerial = "YOUR_TV_SERIAL";
};
```

The backend binds loopback port 18796 (`backendPort`). A systemd socket proxy listens on the chosen
LAN address at port 18795 (`port`); the firewall accepts that port only from the configured
Roku address. It serves token-scoped media, not control routes. The agent controls
the service through `/run/roomcast/control.sock` (group `roomcast`, mode 0660).
Add only the intended client service/account to that group. Use
`config.services.roomcast.package` for client binaries so a package override
applies to both the service and its clients. Address options require DHCP
reservations; automatic discovery and lease-change recovery are not implemented.

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
URLs and handles required provider request headers server-side. HLS initialization
segments and media fragments are joined and passed to FFmpeg with `-c copy`.
Original timestamps are preserved. No frame is decoded or encoded.

Source resolution is lazy: the browser yields a server's current candidates,
playback prepares and verifies them, and only failures advance to another server.
At most eight menu entries and three candidate URLs per server are tried. Browser
contexts close on success, failure or cancellation; no resolved URL is persisted
across play requests. Site changes still require adapter maintenance. Expiry during
an already-playing session currently requires a new play request; seamless URL
renewal and resume are not implemented.

There is one active playback session. Responses share an LRU cache capped at
128 MiB; four upstream jobs and two remux processes may run at once. Each media
object is capped at 32 MiB, and a remux process has a ten-second deadline. A session
expires after six hours. Stop invalidates its token and cancels its work. The
first two segments of a requested media playlist are prefetched. Further segments
are fetched when Roku asks, so the entire episode is not downloaded upfront.

The full VOD playlist remains available for Roku's native seek UI. Absolute
seek-by-text, subtitle selection, recovery after upstream URL expiry and persistent
resume positions are not implemented yet. Pause/resume and stop are supported.
A service restart ends the relay session; request playback again.

## Shared iMessage groups

This project supplies constrained tools, not group authentication. Do not give a
roommate group your existing personal Hermes toolset. Route authenticated sender
and chat IDs to a separate agent runtime with only this MCP server, separate
memory, and no personal browser, shell, files, KB, scheduling or delegation tools.
The group must not be able to change routing/allowlists or invoke the personal
agent. Message routing and replies require an integration-specific acceptance
test before admitting roommates. Mention gating is convenience, not permission.

The browser's request checks are defense in depth; they are not a complete hostile
browser network sandbox. Before expanding supported sites or enabling untrusted
users, isolate browser egress at the OS/network layer too. This release does not
claim a fully deployed multiplayer iMessage security boundary.

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
