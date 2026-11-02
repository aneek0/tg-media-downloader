# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

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

### Removed

- `DownloadOption.mode`: written at 6 call sites, read at one (`services/ytdlp.py` audio
  branch). The `--extract-audio` path now keys off `audio_quality`, which only mp3 options
  set, so the duplicate discriminator is gone. Stale request files that still contain
  `mode` are tolerated: `DownloadOption.from_dict` drops unknown keys.

### Added

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

### Fixed

- Long posts (X/Twitter, pawchive) no longer arrive collapsed and truncated:
  `plain_caption` keeps the author's line breaks instead of flattening the text
  into one paragraph, and text over Telegram's 1024-character caption limit now
  follows the media as a separate message (up to 4096 characters each) instead
  of being cut at character 1000/1024 — the tail links of long posts survive.
  Long captions are likewise replayed from the media cache instead of being
  truncated again. Gallery captions are HTML-escaped for the bot's parse mode,
  so posts containing `&` no longer fail to send.

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
