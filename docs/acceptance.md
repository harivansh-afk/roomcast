# Acceptance on 2026-09-06

Target: a 40-inch Roku Select Series FHD TV, Roku OS 15.3.4, Media Assistant 1.3.1.
Source: The Gentlemen (2024), season 1 episode 1, through Cinejoy's Nebula provider.

- The initial direct URL failed on Roku. Inspection found H.264 High, 8-bit 4:2:0,
  720p video and AAC-LC audio; no video conversion was required.
- The relay selected 1080p under its default cap and repackaged fragmented MP4
  segments into MPEG-TS with FFmpeg stream copy.
- Playlist/first-segment preparation took 0.372 seconds on Spark for that run.
  This excludes browser lookup, Roku startup and buffering; it is not an SLA.
- A separate cold browser resolver run found the episode in 11.833 seconds.
- Roku reported Media Assistant actively playing, no error, position 8.171 seconds
  and duration 4,027.958 seconds. The user independently confirmed playback.
- Pause held at 31.505 seconds. Resume advanced to 34.545 seconds. Playback was
  still error-free at 99.711 seconds in a subsequent check.
- The installed app was launched over ECP; manual app opening is not required.

The test media endpoint was loopback on Spark. Because Spark's existing firewall
had not yet been changed, a temporary SSH/TCP forward on the Mac provided the LAN
listener, restricted to the TV and Spark addresses. No video was rendered or
encoded on the Mac. Direct Spark-to-TV deployment remains a separate acceptance
step after installing the Nix module. No roommate iMessage group has been admitted
or messaged, and no personal-agent permissions were expanded for this test.

The tests cover hostile/redirected URL boundaries, private DNS answers, playlist
rewriting, cache bounds, concurrent fetch deduplication, retries, invalidation,
Roku identity, query encoding, asynchronous stop and HTTP ranges. They do not
establish universal provider compatibility or uninterrupted full-episode playback.


## Playback rewrite, 2026-09-06

Family Guy S01E01 through Lisbon reproduced the missing-audio issue on the same
TV: the deployed service reported `playing` while ECP reported `audio="none"`.

With the rewritten relay, the source's actual 1920x1080 H.264 Main level 4.0 video
and AAC-LC stereo audio were validated across eight segments in 3.51 seconds on
Spark. This excludes browser lookup and TV startup. A first TV attempt caught
false provider CODECS metadata: it advertised level 5.0 and Roku rejected the
master before fetching media. Omitting that hint and publishing measured
parameters allowed startup.

The TV then reported AAC audio and H.264 video, no error, and progressed from
4.163 to 65.804 seconds. Pause held at 24.957 seconds and resume advanced to
35.526 seconds. Hari independently confirmed that picture and sound worked.
Thirty-seven segments were validated during this run, including startup samples
and subsequent TV requests. The test used a temporary Mac TCP forward to a
worktree service on Spark; neither machine recorded the desktop or transcoded the
video. Both temporary processes were stopped after the test.

This establishes the tested episode and controls, not every title or full-episode
playback. Permanent direct Spark delivery is a separate deployment check.

## Latency work, 2026-09-07

Target: Roku H592X / 40R3EX, OS 15.3.4 build 832, installed Roomcast Player
**1.3.4**. The 1.4.0 player builds and passes BrightScript compilation and control
tests, but has not been installed: the developer installer requires its password.
Its startup, replacement, seeking and caption behavior remain pending TV tests.

Casino Royale (2006), TMDB movie 36557, Lisbon, starting at 60 seconds, resolved
to 1722x720 H.264 Main at 23.976 fps with external AAC stereo audio in these runs.
Each run resolved fresh provider URLs. No video was transcoded.

| Measurement | Before (0.1.0) | Relay with init checks and parallel subtitle preparation |
| --- | --- | --- |
| Request to observed TV playback | 16.007, 15.809 s | 11.264, 14.769, 9.524 s |
| Request to confirmed playback | 18.738, 19.439 s | 13.128, 16.590, 10.592 s |
| Preparation, including selected subtitles | approximately 5.0–6.0 s | 1.481, 1.766, 1.916 s |
| Pause/resume response | 0.428–0.658 s | 0.371–0.619 s |

The baseline used the deployed service directly from Spark to the TV. The new
relay used a built package on Spark through a temporary TV-only Mac TCP forward
while deployment was pending. The extra network hop and small sample make these
observations unsuitable for a universal percentage improvement claim. Resolver
time alone varied from 3.710 to 8.215 seconds in the three later runs. TV playback
means ECP-reported advancing audio and video, not measured screen or speaker output.

Two separate preparation-only runs took 1.193 and 1.354 seconds and downloaded
about 3.01 MB each. The relay rejected an oversized variant from its small fMP4
init without downloading that variant's media fragment. The chosen audio and
video segments were fully decoded; every subsequently delivered segment still
requires validation. A ten-second playback check advanced from 62.596 to 72.731
seconds with no TV error and both tracks present; 59 segments had been verified.

The old installed player's paused seek to 20:00 failed confirmation and remained
near 1:04. This is not accepted as working. Subtitle callbacks in the forwarded
test were unavailable because the forward does not preserve the TV's source IP;
the server correctly requires the paired TV address for reports.

The temporary service and forward were stopped. The attempt to restore the
production movie afterwards timed out and was not confirmed successful. On
inspection the production job was stopped and the TV was playing YouTube. The
reported playback failure still needs reproduction; a healthy process is not
proof of movie playback. The first relay update (Roomcast commit 7cf227a) was
deployed by Nix PR #615 at 02:02 UTC; the second relay pass above remains separate.
