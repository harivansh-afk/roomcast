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
