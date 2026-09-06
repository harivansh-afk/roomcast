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
