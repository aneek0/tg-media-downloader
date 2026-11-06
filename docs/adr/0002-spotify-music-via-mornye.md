# ADR 0002: Spotify-link music via mornye as a library

**Status:** Accepted
**Date:** 2026-10-19

## Context

The bot serves media from links (yt-dlp, gallery-dl, direct CDN). Users need
music from Spotify links in lossless. The external tool is
[mornye Module](https://github.com/mornyeapp/Mornye-Python)
(PyPI `mornye`): it resolves Spotify metadata and downloads FLAC through
provider extensions (Deezer, Tidal, Qobuz); nothing is bundled — the bot owner
chooses the extension registry.

Two integration approaches were considered. Spawning a CLI subprocess was
rejected: metadata is needed **before** downloading (inline article, keyboard),
and the spawn + startup overhead on every click of a synchronous CLI is
inconvenient; the `AsyncMornye` library provides native async over a shared
HTTP pool. The decision is based on measurements: import ~0.35 s, album
metadata 0.25–0.5 s in-process.

## Decision

1. **Integration as a library.** One long-lived `AsyncMornye` client in
   `services/mornye.py`, `__aenter__` once per bot session. Downloads go
   through `download_track` / `download_tracks`; extensions and the registry
   live in the owner's `~/.mornye` (MORNYE_REGISTRIES env, as with the CLI).

2. **Single-track metadata via the album endpoint, not `get_track_async`.**
   In the investigated mornye version, the `getTrack` GraphQL path returns a
   degenerate `Unknown` record for **all** track IDs (stale persisted-query
   hash) — a silent fallback in the `TRACK_NOT_FOUND` filter. The working
   workaround (verified, ~0.9 s): HTTP GET the `open.spotify.com/track/<id>`
   page → `og:title` + `/album/<id>` → `get_album_tracks_async(album_id)` →
   match by track ID. Incidentally, an og-404 also validates the link.
   Album/playlist/artist resolution works directly through `get_url_async`.

3. **Default provider is `ext:deezer`.** Verified end-to-end
   (~90 s/track, including Turnstile via headless Chromium — the browser
   dependency remains with the owner, not in the bot). Default enrichment
   (`enrich_providers`) without `qobuy` timeouts: the list is limited, e.g.
   `"deezer", "apple"`. The provider list is the `MORNYE_SERVICES` env var.

4. **Output format is FLAC as-is** (`MORNYE_TRANSCODE` empty → original;
   `mp3` → MP3 320k transcode). Sending uses Send Type `audio` in all cases:
   the Telegram client plays FLAC via `send_audio`. Metadata for the Telegram
   player: `title`/`performer` come from TrackMetadata
   (DownloadArtifact.audio_title/audio_performer), duration from
   `DownloadArtifact.duration`, otherwise hachoir
   (`services/media.py:audio_duration`). Cover art is the embedded FLAC/MP3
   cover via mutagen (`services/delivery.py:_extract_cover`, a temporary JPEG
   deleted after sending); without an embedded cover, the owner's per-user
   thumbnail is used.

5. **Collection delivery — cover + audio media groups.** Before the track
   group, a cover photo is sent (as in deezload2bot): the album is sent with
   `reply_media_group` in chunks of 10 (Telegram limit), each item an
   `InputMediaAudio` with title/performer/embedded cover. A single track is a
   single `reply_audio` without a separate photo.

6. **Partial-failure tolerance.** mornye returns per-track success/failure
   visibility; we count what was downloaded, report the list of failures, and
   the final report is assembled after all attempts (retry).

7. **Collection cap — 50 tracks** (convention
   `services/gallery.py:MAX_GALLERY_PHOTOS`), with an explicit "downloaded N
   of M" message (unlike the gallery's silent cutoff).

8. **Inline mode.** Both inputs: track and album/playlist (truncated by the
   cap). Metadata for the inline article is og-meta (`track`) or
   `get_url_async` (collections), without downloading. The shared cooldown
   also covers inline requests.

9. **Cache — single tracks only.** Album/playlist are not cached in Media
   Cache in phase 1 (the trade-off is clear: 50 file_ids per playlist for a
   rare re-request does not justify the added complexity in `media_cache.py`).

10. **Direct Deezer links.** deezer.com/track|album|playlist are recognized
    by the same intake (`is_mornye_url`, replacing `is_spotify_url`):
    resolution via `ExtensionMetadataClient.get_url_async` (the `ext:deezer`
    extension provides name, tracks, cover), download via the same
    DownloadWorker through `ext:deezer`. Spotify metadata is not involved.

11. **Extension network guard.** The mornye Node runtime blocks "private"
    addresses; behind a fakeip VPN (sing-box TUN), everything external to the
    extensions looks like 198.18.x.x and is blocked, while the resolver
    returns placeholder 503s from api.zarz.moe. The workaround is
    `MORNYE_EXT_ALLOW_PRIVATE_NETWORK=1`. The `/dl/dzr` resolver is also
    flaky (ConnectError/503 about 1 in 3) — `track_max_retries=2` in
    DownloadOptions.

12. **Turnstile verification (zarz-v2).** Downloading requires a signed-session
    signature; if there is no session, pydoll starts Chromium — `CHROME_PATH`
    (helium wrapper) and `HELIUM_USER_FLAGS` (--no-sandbox etc.) are required
    in the bot process environment. Auto-solve takes ~40 s, while the
    extension bridge call has a 60 s limit — so an authentication failure
    caused by a missing browser sets a file backoff
    (`signed_sessions/.*.auth-backoff.json`, 5 min × 3 per failure, up to
    4 h): the bot fails with "verification paused" even after the browser is
    fixed. Remedy: fix the cause, then delete the backoff file (the gateway
    was not consulted: the failure was local) and restart.

## Risks and consequences

- **Upstream defect** in single-track metadata is documented and worked
  around; when upgrading mornye, re-check whether `getTrack` has been fixed
  and remove the workaround (see the test for the persistent failure).
- Downloading depends on external extensions and Turnstile — slow and
  unstable; all processing during degradation stays behind the existing
  exception boundary (`executor`/`inline_flow`).
- mornye is a heavier library than a subprocess: one dependency (`mornye` in
  pyproject). The opt-out is built into the configuration itself: if the owner
  does not set `MORNYE_SERVICES`, the bot does not recognize Spotify links and
  the code does not run.
