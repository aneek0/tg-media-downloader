<div align="center">

# tg-media-downloader

**Telegram bot: direct links, `yt-dlp` and `gallery-dl` media → back to Telegram.**

Fork of [All-Url-Uploader](https://github.com/kalanakt/All-Url-Uploader), reworked for self-hosting:
inline mode, Twitter/X support via `gallery-dl`, split proxies, local Bot API server.

Built with `aiogram`, `yt-dlp`, `gallery-dl`, and `uv`.


[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3110/)
[![CI](https://github.com/aneek0/tg-media-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/aneek0/tg-media-downloader/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/github/license/aneek0/tg-media-downloader)](LICENSE)

[Changelog](CHANGELOG.md) · [Security policy](SECURITY.md) · [Issues](https://github.com/aneek0/tg-media-downloader/issues)

</div>

The bot accepts a direct file URL or a supported media link, downloads it with the right tool for the
job, and sends the result back to Telegram with the correct media type and metadata.

## What It Handles

- direct file links
- `url|filename`
- `url|filename|username|password`
- `url * filename`
- YouTube quick audio and quick video downloads
- format selection for supported `yt-dlp` sources
- **Twitter/X posts via `gallery-dl`** — photos, videos, albums, and text-only posts, including 18+ posts with cookies
- **Gallery post links** — direct-message gallery deliveries use the post text (or filename) as a link to the original post
- **Parallel gallery albums** — local multi-file downloads overlap up to 3 files per album and 6 across the bot,
  preserve album order and source headers, and fall back to gallery-dl if direct CDN retrieval fails
- **inline mode** — use it in any chat, no need to forward links to the bot first
- **Deezer, Spotify, and Tidal catalogue search** — text queries in private chat or inline; tracks download in the
  selected FLAC/MP3 format through mornye
- **Tidal links** — `listen.tidal.com`/`tidal.com` tracks, albums, and playlists download in private chat and
  inline through mornye; artist/mix links get an explicit unsupported-link message instead of an HTML-download error
- **SoundCloud links** — short/mobile/canonical URLs resolve through the mornye SoundCloud extension; playable
  native AAC streams use `ext:soundcloud`, while unavailable streams or extension failures retain yt-dlp fallback
- **Artist Cards** — cover, fan metadata, a separate TOP 30 view, up to 50 albums, per-track downloads,
  album GET ALL/ZIP, and follows. Private inline selections insert the artist URL and receive a bot-authored DM card;
  selections in other chats retain editable inline cards
- **Release follows** — `/follow`, `/follows`, and `/unfollow`; new-release checks run every six hours per follower
- per-user `/settings` for text search, default catalogue provider, FLAC/MP3 delivery, and CD vs Hi-Res
  (Hi-Res is only used where the provider has a 24-bit master; everything else stays lossless CD)

## Inline Mode

Enable inline mode for the bot via [@BotFather](https://t.me/BotFather) (`/setinline`). Also run
`/setinlinefeedback` and choose **Enabled**: Telegram sends `chosen_inline_result` only with feedback enabled,
and downloads/Artist Cards cannot start without that update. Then:

- type `@yourbot <url>` in any chat — tweet photos come back as direct Twitter CDN results (no
  download step), text-only tweets are ready text results, and other supported links download on tap with a cancel button
- search the catalogue with `@yourbot <query>` or scope it with `.trk`, `.a`, `.art`, `.pla`;
  choose Deezer/Spotify/Tidal in `/settings`; artist results open an Artist Card with TOP 30/ALBUMS/FOLLOW controls
- repeated track selections reuse the cached Telegram audio `file_id`; the shared inline runner skips
  both the provider download and the DM upload
- for multi-track downloads, the selected/first track replaces the inline message; its DM copy arrives first,
  then the remaining tracks follow in their original order

## Bot Commands

The bot registers this command menu automatically on startup; no manual BotFather `/setcommands` step is needed.

- `/start` - welcome message, shortcuts, and usage guidance
- `/help` - supported link formats and flow overview
- `/about` - runtime details, repo link, and project notes
- `/settings` - text-search, catalogue-provider, audio-format, and CD/Hi-Res preferences
- `/follow <artist-url>` - follow a Deezer artist for new-release DMs
- `/follows` - list followed artists with unfollow buttons
- `/unfollow` - open the followed-artists list and remove follows

## Quick Start

1. Clone the repository and move into it:

```bash
git clone https://github.com/aneek0/tg-media-downloader.git
cd tg-media-downloader
```

2. Create a `.env` file from the example and fill in the required values:

```bash
cp .env.example .env
```

`BOT_TOKEN` and `OWNER_ID` are required; everything else has a sane default. See the
[Environment](#environment) table below.

3. Install dependencies:

```bash
uv sync --group dev
```

4. Start the bot:

```bash
uv run python bot.py
```

## Environment

- `BOT_TOKEN` - required Telegram bot token
- `OWNER_ID` - required Telegram user ID for the bot owner
- `AUTH_USERS` - optional comma-separated list of user IDs that bypass the cooldown
- `CHUNK_SIZE` - optional direct-download chunk size; values below `1024` are treated as kilobytes for backward compatibility
- `DOWNLOAD_THREADS` - optional; number of parallel HTTP connections for the raw (direct) downloader, clamped to 16 (default 16); `1` disables parallel segments and downloads the file in a single stream
- `DOWNLOAD_LOCATION` - optional base directory for temporary downloads and uploads
- `PROCESS_MAX_TIMEOUT` - optional hard timeout in seconds for one yt-dlp / gallery-dl process (default 3700)
- `AUTO_BEST_QUALITY` - optional; when `true` (default) yt-dlp links skip the format-selection keyboard and download at the best quality within `MAX_HEIGHT`
- `MAX_HEIGHT` - optional; maximum video height for auto downloads (default 1080)
- `GALLERY_PROBE_TIMEOUT` - optional; gallery-dl probe timeout for Twitter/X posts in seconds (default 15)
- `REQUEST_COOLDOWN_SECONDS` - optional per-user cooldown window in seconds (default 3); `OWNER_ID` and `AUTH_USERS` bypass it
- `MAX_UPLOAD_BYTES` - optional; downloads larger than this are skipped with a friendly message (default 1992294400 ≈ 1.9 GB, the local Bot API server limit; set 52428800 for the cloud Bot API)
- `VERIFY_SSL` - optional; set false only for broken TLS interception (default true)
- `HTTP_PROXY` - optional proxy URL used for direct downloads, yt-dlp, and gallery-dl
- `TELEGRAM_PROXY` - optional proxy URL for the Telegram API connection only; falls back to `HTTP_PROXY` when unset. Ignored when `TELEGRAM_API_URL` points to a self-hosted server (reached directly)
- `TELEGRAM_API_URL` - optional base URL of a self-hosted local Bot API server (e.g. `http://localhost:8081`); raises the bot upload limit from 50 MB to 2000 MB. Run the official `telegram-bot-api` binary with `--local --api-id ... --api-hash ...` (keys from my.telegram.org) next to the bot; leave empty for the cloud Bot API
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` - not read by the bot itself; used by the local Bot API server (see [Docker Compose](#docker-compose-recommended)) — the same keys from my.telegram.org
- `TWITTER_COOKIES_FILE` / `TWITTER_COOKIES` - optional; path to a Netscape-format cookies.txt exported from a logged-in X/Twitter account, or the file content pasted into `.env` as a multiline value; enables downloads of 18+ (NSFW) posts. Export cookies with a browser extension ("Get cookies.txt LOCALLY") while logged in to x.com.
- `SEARCH_PROVIDER` - default catalogue provider: `deezer` (default), `spotify-web`, or `tidal-web`
- `FOLLOW_POLL_SECONDS` - release-check interval in seconds (default `21600`, six hours)
- `MORNYE_SERVICES` - comma-separated provider extensions in download priority order; required for catalogue audio. With the extensions installed, `ext:tidal-web,ext:qobuz-web,ext:deezer` prefers the currently reliable Tidal path and keeps Deezer as fallback
- `MORNYE_ENRICH_EXTENSIONS` - set `0` (recommended) to skip optional metadata fan-out to every installed JavaScript extension; built-in Deezer/Apple enrichment remains, and Tidal downloads avoid a separate Qobuz Turnstile challenge. Set `1` only if provider-specific extra tags are worth the extra browser runs
- `MORNYE_EXT_ALLOW_PRIVATE_NETWORK` - set `1` behind a FakeIP/TUN VPN such as sing-box so catalogue search and downloads can reach provider endpoints; enable only for trusted installed extensions
- `MORNYE_REGISTRIES` - optional comma-separated mornye extension registry JSON URLs (see `.env.example` for the default registry)
- `CHROME_PATH` - optional Chromium executable for ZARZ Turnstile authentication. Docker sets `/usr/bin/chromium`; on bare metal, install `chromium` and `xvfb` (Mornye starts the virtual display on demand) or point this at another Chromium-based browser such as Helium
- `MORNYE_QUALITY` - optional default audio tier: `LOSSLESS` (CD, default) or `HI_RES_LOSSLESS` (Hi-Res, with per-track fallback); users can override it in `/settings`
- `MORNYE_TRANSCODE` - optional; empty keeps the original FLAC, `mp3` transcodes to MP3 320k through ffmpeg
- `SPOTIFY_COLLECTION_CAP` - optional maximum tracks downloaded per Spotify album/playlist/artist collection (default 50)

When mornye is enabled, startup synchronizes extensions and checks/authenticates each enabled provider's
signed session before polling. A background pass renews sessions every five minutes without user requests;
existing sessions refresh over HTTP, while missing/expired ones may require a Chromium Turnstile challenge.

For a server with roughly 600 MB of free RAM, configure 1–2 GB of swap before enabling Mornye. Turnstile starts several Chromium processes, and simultaneous downloads or provider challenges can otherwise trigger OOM kills. Keep `MORNYE_ENRICH_EXTENSIONS=0` and persist `/home/app/.mornye` so valid signed sessions avoid browser launches.

## Project Layout

- root runtime entrypoints: `bot.py`, `app.py`, `config.py`
- routers: `routers/`
- services: `services/`
- shared helpers and models: `utils/`
- tests: `tests/`

## Deployment

### Docker Compose (recommended)

Runs the bot together with a local Bot API server and lifts the upload limit to 2000 MB:

```bash
cp .env.example .env   # fill BOT_TOKEN, OWNER_ID, TELEGRAM_API_ID, TELEGRAM_API_HASH
docker compose up -d
```

`compose.yml` pulls the published image `ghcr.io/aneek0/tg-media-downloader:latest`; comment in
`build: .` and comment out the `image:` line to build locally instead.

Persistent state lives in the `downloads` volume (`/app/DOWNLOADS`) and the `mornye` volume
(`/home/app/.mornye`, installed extensions and signed sessions).

The bot service forces `CHROME_PATH=/usr/bin/chromium` even if `.env` contains a host-specific Helium path, and sets `shm_size: "1gb"`. This is a tmpfs size limit, not an immediate 1 GB RAM allocation.

The local Bot API server requires the bot to run on the same host once before files are downloadable
by URL; the bot already handles this (local mode files are uploaded by the bot itself).

### Docker (manual)

Build and run the container with your existing `.env` file:

```bash
docker build -t tg-media-downloader .
docker run --env-file .env --env CHROME_PATH=/usr/bin/chromium --shm-size=1g \
  -v tgmd-downloads:/app/DOWNLOADS -v tgmd-mornye:/home/app/.mornye \
  tg-media-downloader
```

Or skip the build and use the published image:

```bash
docker run --env-file .env --env CHROME_PATH=/usr/bin/chromium --shm-size=1g \
  -v tgmd-downloads:/app/DOWNLOADS \
  -v tgmd-mornye:/home/app/.mornye \
  ghcr.io/aneek0/tg-media-downloader:latest
```

`--env CHROME_PATH=...` must follow `--env-file` when reusing a host `.env`: it prevents a desktop-only Helium path from leaking into the container.

The image is multi-stage, runs as the non-root `app` user under Tini, includes Node.js for Mornye extensions plus Chromium/Xvfb for signed-session challenges, and pins dependencies from `uv.lock`.

### systemd (bare metal)

```bash
sudo cp examples/tg-media-downloader.service /etc/systemd/system/
sudo mkdir /etc/tgmd && sudo cp .env /etc/tgmd/tg-media-downloader.env
sudo systemctl daemon-reload
sudo systemctl enable --now tg-media-downloader
```

The unit assumes the checkout lives at `/opt/tg-media-downloader` (with `.venv` from `uv sync`) and
runs under a dynamically provisioned user; adjust paths to your setup.

Install `chromium` and `xvfb` on the host when Mornye is enabled. Set `CHROME_PATH` in the unit's environment file if the executable is outside Mornye's standard locations.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `WEBPAGE_CURL_FAILED` on tweet videos | Telegram's own fetcher refuses `video.twimg.com`. Tweet videos are download-on-tap articles — tap the result and let the bot download and upload it. Photos keep the instant CDN path. |
| Twitter/X: "login required" or 18+ posts fail | `TWITTER_COOKIES(_FILE)` missing or the session expired. Re-export cookies.txt from a logged-in browser and update the value. |
| Uploads fail above 50 MB | No local Bot API server: `TELEGRAM_API_URL` is unset, the server is down, or it was started without `--local` (compose sets it via `TELEGRAM_LOCAL=1`). Also set `MAX_UPLOAD_BYTES=1992294400` only if you run the local server; keep `52428800` for the cloud Bot API. |
| Direct link arrives as a broken document / `text/html` rejected | The URL returns an HTML page, not a file. The bot rejects it on purpose — send it through the yt-dlp path instead (just paste the link; the probe routes it). |
| `yt-dlp` or `gallery-dl` suddenly fails on a site | Extractors rot as sites change. Update the tools: `uv lock --upgrade-package yt-dlp --upgrade-package gallery-dl && uv sync` (updates `uv.lock`; commit the refresh), then restart the bot. |
| Bot replies "File too large to upload" | The resolved media exceeds `MAX_UPLOAD_BYTES`. Raise it only up to the 2000 MB local Bot API limit, or lower the requested quality. |

## Checks

Run the checks locally (CI runs the same):

```bash
uv run pytest
uv run ruff check .
uv run ty check
```
