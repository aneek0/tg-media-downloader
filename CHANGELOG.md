# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [3.3.0] - 2026-11-08

### Added

- Direct-message gallery deliveries now use the post text or filename as a bold link to the original post.
- Inline results delivered in someone else's chat now name the bot by handle: the album spillover caption reads
  "check your DM with @yourbot" (or "open @yourbot and send the link" when the DM cannot be delivered) instead of
  an unnamed "the bot". The handle is resolved once at startup via `get_me`; if that fails, the prompts fall back
  to the previous generic wording. Inline `/settings` alerts name the bot the same way.
- Telegram's command menu is registered automatically at startup for all commands handled by the bot.
- Docker deployments now include Chromium, Xvfb, browser fonts, and Tini for ZARZ Turnstile authentication.
  Compose forces the container browser path, allocates up to 1 GB of shared memory, preserves signed sessions,
  and documents swap guidance for low-memory servers.

### Removed

- Custom per-user thumbnails, including `/thumb`, `/delthumb`, implicit photo capture, and thumbnail overrides
  during delivery. Embedded FLAC/MP3 cover art is still extracted and attached to audio.

### Changed

- Reduced the stock per-user cooldown between links in private chat from one hour to three seconds;
  `REQUEST_COOLDOWN_SECONDS` still overrides it, and the owner plus `AUTH_USERS` remain exempt.
- Reduced the per-user inline catalogue search interval from 1.5 seconds to 0.5 seconds.
- Multi-file gallery downloads now retrieve up to 3 files concurrently per album, capped at 6 across the bot;
  source request headers and album order are preserved, with the sequential gallery-dl path as fallback.
- Artist Cards now open on a compact TOP 30 / ALBUMS menu; Deezer fan metadata is the final caption line, and
  track downloads appear only after opening TOP 30.
- Artist discographies now separate full albums from singles/EPs with a section divider; release buttons use plain
  titles with explicit 🅴 markers instead of repeated disc icons.
- Single-track release cards no longer show a redundant GET ALL button; the individual track download and ZIP
  controls remain available.

### Fixed
- Inline release ZIP uploads now use the artist and release title in their Telegram filename instead of `album.zip`.
- Private inline artist selections now insert the artist URL so normal DM intake answers with a bot-authored Artist
  Card; selections in other chats retain the existing editable inline card.
- Artist inline results now use named articles with cover thumbnails, keeping artist names visible in clients that
  hide photo-result titles.
- Deezer Artist Cards deduplicate public/extension TOP IDs and fill short lists with unique album tracks up to 30.
- Single gallery media now uses the same direct HTTP downloader as albums before falling back to `gallery-dl`,
  fixing `403 Forbidden` failures on Pawchive CDN video links.

- Inline tap-to-download results now upload the first downloaded artifact to the user's DM and edit the
  inline message with the returned `file_id`. Telegram rejects new-file uploads on inline message edits,
  which previously made downloaded TikTok/gallery videos fail with "invalid message content".

- Pinned pydoll to 2.27.0: pydoll 3.0.0 loaded the ZARZ challenge but never clicked the Turnstile checkbox,
  leaving signed-session warm-up to time out. Verified with Chromium/Xvfb on the headless Debian server.

## [3.2.0] - 2026-11-06

### Added

- Deezer/Spotify/Tidal catalogue search in private messages and inline mode, with scoped track/album/artist/playlist
  queries, cached results, and per-user provider selection.
- Inline Artist Cards: cover/listener metadata, TOP 30, ALBUMS 50, follow toggle, and one-tap track downloads.
- Album cards with per-track buttons, GET ALL, and ZIP delivery; per-user FLAC/MP3 audio preference.
- Per-user CD/Hi-Res toggle in `/settings`: Hi-Res requests `HI_RES_LOSSLESS`, verifies the returned file is
  really 24-bit, re-downloads upsampled "fake hi-res" at honest lossless, and falls back per track when the
  provider has no 24-bit master. Hi-Res cache entries are keyed separately so a CD request is never answered
  with a 24-bit file (or the reverse).
- Tidal track, album, and playlist URLs (`listen.tidal.com`, `tidal.com`, `www.tidal.com`) now classify as music
  and resolve through mornye's Tidal metadata client in private chat and inline mode, including cover thumbnails
  in inline results. Tidal artist/mix links are rejected with a clear hint.
- Tidal catalogue search via `tidal-web`, selectable in `/settings` for private and inline queries, with track,
  album, artist, and playlist filters. Results without explicit external URLs now stay on Tidal instead of falling
  back to malformed Deezer links.
- SoundCloud short, mobile, and canonical URLs now classify as music in DM and inline mode. Metadata resolves
  through the SoundCloud extension's `handleUrl`; native AAC downloads use exact SoundCloud IDs, and unplayable
  streams or extension/metadata failures fall back to the existing yt-dlp audio path instead of failing the link.
- Enabled mornye providers authenticate signed sessions before polling and renew them every five minutes in
  the background, using fresh scoped clients and honoring mornye's authentication backoff.
- Six-hour per-user artist release checks plus `/follow`, `/follows`, `/unfollows`, and `/settings`.
- `GLOSSARY.md`: domain vocabulary (Request, Option, Artifact, Media Cache, Preview Store,
  Telegram Fetch, Direct Upload, …) with `_Avoid_` lists, one section per delivery flow.
- Rich messages (Bot API `sendRichMessage`) for posts that overflow the 1024-character
  caption limit: a photo post with long text goes out as **one** message with the image
  and the full text (up to 32 768 characters), in DM and as an inline `Full post` result
  (`InputRichMessageContent`). Servers or chats without rich-message support keep the
  photo + follow-up-text fallback. Rich messages expose no re-usable `file_id`, so they
  are not written to the media cache and a repeat re-sends them from the CDN.

- Rich block conversion (`services/delivery.py:rich_blocks`): a plain caption becomes
  real blocks — all-caps section lines → `<h3>`, `1. …` runs → `<ol>`, `- …` runs →
  `<ul>`, paragraphs with `<br>` for single line breaks. Rich HTML drops bare newlines,
  so without this the whole post collapsed into one paragraph.

### Changed
- aiogram upgraded from yanked `3.29.0` to `3.31.0`, which is now the minimum supported version: this avoids its
  severe RichBlock parsing slowdown and guarantees the Rich Message API imported by the bot.
- The release container includes Node.js for mornye extensions; Compose persists installed extensions and signed
  sessions in a dedicated `mornye` volume.
- Release tags now enforce the same pylint 10/10 gate as main-branch CI.

- The DM catalogue-search keyboard now uses two columns for Tracks/Albums and Artists/Playlists, with a
  full-width Global search button at the bottom that preserves the unscoped query.

- Upstream music library renamed: `spotiflac` → `mornye` (repo
  [mornyeapp/Mornye-Python](https://github.com/mornyeapp/Mornye-Python), PyPI
  `mornye`). Internal API surface used by the bot is unchanged
  (`DownloadWorker`, `DownloadOptions`, `SpotifyMetadataClient`,
  `ExtensionMetadataClient`, `ExtensionManager`); all env vars renamed
  `SPOTIFLAC_*` → `MORNYE_*` (`MORNYE_SERVICES`, `MORNYE_QUALITY`,
  `MORNYE_TRANSCODE`, `MORNYE_EXT_ALLOW_PRIVATE_NETWORK`). Module renamed
  `services/spotiflac.py` → `services/mornye.py` (`MornyeService`),
  request type string `"spotiflac"` → `"mornye"`; stale request files with
  the old literal are swept like any other 24 h-old state. Old `~/.spotiflac`
  extension registry is not reused — mornye re-syncs into `~/.mornye` on
  first start.

- One download pipeline (`services/pipeline.py`): the probe ladder (music →
  gallery → auto-best/YouTube → yt-dlp → direct) and downloader dispatch now
  live in one module; intake, the DM executor and inline taps all use it.
- Request construction and cleanup moved to `services/requests.py`: named
  per-flow constructors, typed per-variant payloads (`utils.models.RequestType`
  + payload dataclasses), and one `lifecycle` context manager that deletes the
  token and work directory.
- Uploads split into `services/delivery.py` (Telegram sends, thumbnail,
  metadata, album chunking) and `services/status.py` (chat/inline status
  surface, progress throttling); `services/telegram_uploads.py` is gone.
- Source classification extracted to `services/sources.py` (`SourceKind`,
  `classify`, direct-download option builder).
- Inline big-video delivery now runs through the same status surface as every
  other flow: the poster-photo message is `media_message=True`, so download and
  upload progress edits are dropped instead of failing against a media message,
  while failures still edit the poster into an explicit error. `poster_url` and
  `video_url` moved from raw `info` keys into `InlineMediaPayload`.
- One dependency for handlers and flows: `services/session.py:Session` (frozen
  dataclass: `settings`, `cooldown`, `request_store`, `thumbnail_store`,
  `media_cache`, `mornye`) built once by `build_session(...)` and injected as
  `workflow_data["session"]`. Router handlers and the pipeline/executor/inline
  flows now take `session` instead of five separate stores, so a new shared
  service is a field, not another parameter on every signature.

### Removed

- `DownloadOption.mode`: written at 6 call sites, read at one (`services/ytdlp.py` audio
  branch). The `--extract-audio` path now keys off `audio_quality`, which only mp3 options
  set, so the duplicate discriminator is gone. Stale request files that still contain
  `mode` are tolerated: `DownloadOption.from_dict` drops unknown keys.

### Fixed

- Catalogue inline handlers now return `UNHANDLED` for URL queries and non-artist chosen results, allowing the
  original inline downloader to start instead of leaving the message forever at “Processing your link…”.
- Re-tapping the active `/settings` button now answers "Already selected" without an edit, instead of raising
  `TelegramBadRequest: message is not modified`.
- Private-message intake now ignores the inline placeholder `Processing your link…` (including punctuation/case
  variants) instead of treating it as a text search and answering with the catalogue-search menu. Ordinary text
  searches remain available.
- Catalogue provider network/unavailability failures now return a dedicated retryable inline error instead of the
  generic search failure or exposing internal provider details.
- TikTok photo posts keep using gallery-dl (including `imagePost` galleries), while `videos=false` makes video
  posts fall through to yt-dlp with H.264 preferred over H.265 for Telegram-compatible playback. This avoids
  gallery-dl's extensionless `.mp4` documents without disabling TikTok photo galleries.
- Subprocess calls without a progress callback now drain stdout/stderr with `Process.communicate()`, so yt-dlp
  probe JSON larger than `StreamReader`'s 64 KiB `readline()` limit no longer fails YouTube/Shorts requests with
  `Separator is found, but chunk is longer than limit`.
- Repeated inline catalogue, Artist TOP, and album-track selections now reuse the cached Telegram audio
  `file_id` before invoking mornye; only singleton audio entries take this fast path.
- Artist album buttons now map mornye's real `TrackMetadata.title` and `external_url` fields, so opening an
  album builds its track/GET ALL/ZIP/BACK controls instead of raising `AttributeError: ... no attribute 'name'`.
- Multi-track inline downloads now upload/edit the selected first track before DM spillover, so the private-chat
  sequence is first track → remaining album order. If spillover fails, the inline caption switches to the
  existing open-the-bot fallback instead of claiming that the remaining files are in DM.
- Embedded mornye artwork uses the bot's `aiohttp` transport with bounded retries. This avoids the repeated
  15-second `httpx.ReadTimeout` cover attempts on this host while preserving embedded FLAC artwork.
- Documented BotFather `/setinlinefeedback` as required for tappable inline downloads and recommended
  `ext:tidal-web,ext:qobuz-web,ext:deezer` after repeated Deezer `ECONNRESET` failures. Added
  `MORNYE_ENRICH_EXTENSIONS=0` to the example configuration: optional JavaScript metadata fan-out otherwise
  asks Qobuz after a Tidal download and launches a second, separately authenticated Turnstile browser.

- Long posts (X/Twitter, pawchive) no longer arrive collapsed and truncated:
  `plain_caption` keeps the author's line breaks instead of flattening the text
  into one paragraph, and text over Telegram's 1024-character caption limit now
  follows the media as a separate message (up to 4096 characters each) instead
  of being cut at character 1000/1024 — the tail links of long posts survive.
  Long captions are likewise replayed from the media cache instead of being
  truncated again. Gallery captions are HTML-escaped for the bot's parse mode,
  so posts containing `&` no longer fail to send.
- Python distributions now include `bot.py` and declare `yt-dlp` directly, so an installed wheel contains the entry
  module and the downloader used by the bot.
- Album ZIP uses the persisted browse name instead of a nonexistent `StoredRequest.source_name`, so ZIP delivery no
  longer crashes or leaks its request state.
- Catalogue audio caches now key on the effective FLAC/MP3 output as well as the CD/Hi-Res tier; changing audio format
  cannot replay an incompatible file.
- Inline catalogue audio and ZIP downloads whose DM upload is refused now finish with the open-bot fallback instead
  of leaving the inline result processing forever.
- Tidal Artist Cards can persist follows, and Follow toggles immediately redraw the visible button state.
- Deezer top-track transport timeouts become retryable provider errors, so follow polling skips that artist and keeps
  checking other artists and later rounds.
- SoundCloud collection tracks keep their own URLs for per-track yt-dlp fallback instead of re-downloading or
  mislabeling the whole collection.

## [3.1.0] - 2026-10-05

First tagged release of the fork: everything below since the fork is included.

### Added

- CI (GitHub Actions): pytest + pylint (10/10 gate) on every push/PR, with uv caching.
- Release pipeline: on `v*` tags — tests, multi-arch Docker image pushed to
  `ghcr.io/aneek0/tg-media-downloader`, GitHub Release with generated notes.
- Dependabot: weekly dependency (pip), GitHub Actions, and Docker base-image updates.
- Multi-stage Dockerfile: builder on the official uv image, runtime on `python:3.11-slim`,
  non-root `app` user, `DOWNLOADS` declared as a volume.
- `compose.yml`: bot + local Bot API server (`aiogram/telegram-bot-api`) for 2000 MB uploads,
  persistent state volumes.
- systemd unit example for bare-metal deployments (hardened: DynamicUser, ProtectSystem, seccomp).
- `SECURITY.md`: vulnerability reporting and sensitive-data handling (`TWITTER_COOKIES`, bot token).
- README: full environment table (incl. `PROCESS_MAX_TIMEOUT`, `AUTO_BEST_QUALITY`,
  `GALLERY_PROBE_TIMEOUT`), deployment section (Compose/Docker/ghcr/systemd), troubleshooting table.
- `GALLERY_PROBE_TIMEOUT` added to `.env.example`.
- `DOWNLOAD_THREADS` setting (default 16, clamped to 16): direct-link downloads are fetched over
  parallel HTTP range requests; servers without range support, files below 1 MiB, and
  `DOWNLOAD_THREADS=1` keep the previous single-stream behaviour.
- yt-dlp delegates plain HTTP/HTTPS formats to `aria2c` when the binary is available
  (connections per server driven by `DOWNLOAD_THREADS`); HLS/DASH fragment protocols stay on
  the native path. The Docker image ships `aria2`; without the binary behavior is unchanged,
  and Telegram status progress keeps working by parsing aria2c output lines.
- Twitter/X post support via `gallery-dl` (photos, videos, albums) with NSFW cookies support
  (`TWITTER_COOKIES_FILE` / `TWITTER_COOKIES`) and friendly errors.
- Inline mode: `@bot <url>` in any chat. Tweet media returns as direct Twitter CDN inline results;
  other links download on tap with a cancel button. Album media beyond the inline result limit goes
  to the user's private chat with the bot.
- `TELEGRAM_PROXY` setting for the Telegram API connection only, separate from `HTTP_PROXY` used
  for downloads (yt-dlp, gallery-dl, direct). A local Bot API server (`TELEGRAM_API_URL`) is always
  reached directly.
- Caption format for inline tweet media: status URL, author, content.
- `MAX_UPLOAD_BYTES` setting: downloads that cannot be uploaded are skipped with a friendly message
  before the download starts.
- Media cache for resolved Twitter URLs, avoiding re-resolution for repeated inline queries.
- Tests: inline flow, media cache, direct downloads, request store, proc progress.
- TikTok and other page URLs in inline mode: links yt-dlp can resolve now download through
  yt-dlp instead of a plain HTTP GET that shipped the HTML page as a broken document.
  Direct downloads of `text/html` responses are rejected with a clear error.
  Auto-quality now sorts by resolution (`res`) instead of filtering by height, keeping
  portrait video (e.g. TikTok 1080x1920) at full quality instead of dropping it to 540p.

### Fixed

- Inline video results for Twitter/X no longer fail with `WEBPAGE_CURL_FAILED`: Telegram's own
  fetcher refuses `video.twimg.com`, so videos become download-on-tap articles that run the bot's
  regular download-and-upload flow (same as every other link). Photos keep the instant CDN path.
  The tapped video is matched to the downloaded file by its expected `{tweet_id}_{num}.{extension}`
  name, so mixed photo/video albums pick the right item.
- First status-progress update is no longer swallowed when `time.monotonic()` uptime is below
  the throttle interval (fresh CI runners / short-lived processes): the first progress line
  for a download now always reaches the Telegram status message.

### Changed

- Forked from [kalanakt/All-Url-Uploader](https://github.com/kalanakt/All-Url-Uploader); upstream
  docs site removed in favor of this file.
- Single executor flow for request execution (one place instead of per-router ad-hoc logic).
- Hermetic config tests via `PYTHON_DOTENV_DISABLED`.
- Parallel direct-download segments retry in place up to 3 times, resuming from the last
  written byte, before the whole file falls back to a single-stream restart.
- Version bumped to 3.1.0.

### Removed

- `docs/` external documentation site (Next.js/Nextra), `.hintrc`, `CODE_OF_CONDUCT.md`,
  `CONTRIBUTING.md`, GitHub Actions workflows (`.github/`), Renovate config, Heroku `app.json`,
  Fiverr banner, upstream deploy buttons, contributor table workflow, FUNDING.yml.

## [3.0.0] and earlier

See the upstream repository history:
<https://github.com/kalanakt/All-Url-Uploader/commits/main>
