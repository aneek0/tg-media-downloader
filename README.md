<div align="center">

# tg-media-downloader

**Telegram bot: direct links, `yt-dlp` and `gallery-dl` media → back to Telegram.**

Fork of [All-Url-Uploader](https://github.com/kalanakt/All-Url-Uploader), reworked for self-hosting:
inline mode, Twitter/X support via `gallery-dl`, split proxies, local Bot API server.

Built with `aiogram`, `yt-dlp`, `gallery-dl`, and `uv`.

[![CI](https://github.com/aneek0/tg-media-downloader/actions/workflows/ci.yml/badge.svg)](https://github.com/aneek0/tg-media-downloader/actions/workflows/ci.yml)
[![CodeQL](https://github.com/aneek0/tg-media-downloader/actions/workflows/codeql.yml/badge.svg)](https://github.com/aneek0/tg-media-downloader/actions/workflows/codeql.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/release/python-3110/)
[![License](https://img.shields.io/github/license/aneek0/tg-media-downloader)](LICENSE)

[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Docker](Dockerfile) · [Issues](https://github.com/aneek0/tg-media-downloader/issues)

</div>

The bot accepts a direct file URL or a supported media link, downloads it with the right tool for the
job, and sends the result back to Telegram with the correct media type, metadata, and optional custom
thumbnail.

## What It Handles

- direct file links
- `url|filename`
- `url|filename|username|password`
- `url * filename`
- YouTube quick audio and quick video downloads
- format selection for supported `yt-dlp` sources
- **Twitter/X posts via `gallery-dl`** — photos, videos, and albums, including 18+ posts with cookies
- **inline mode** — use it in any chat, no need to forward links to the bot first
- custom per-user thumbnails with `/thumb` and `/delthumb`

## Inline Mode

Enable inline mode for the bot via [@BotFather](https://t.me/BotFather) (`/setinline`), then:

- type `@yourbot <url>` in any chat — tweet media comes back as inline results (direct Twitter CDN
  links, no download step), other supported links download on tap with a cancel button
- media from an album beyond the inline result limit is sent to your private chat with the bot

## Bot Commands

- `/start` - welcome message, shortcuts, and usage guidance
- `/help` - supported link formats and flow overview
- `/about` - runtime details, repo link, and project notes
- `/thumb` - show the currently saved custom thumbnail
- `/delthumb` - remove the saved custom thumbnail

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
- `DOWNLOAD_LOCATION` - optional base directory for temporary downloads and uploads
- `MAX_HEIGHT` - optional; maximum video height for auto downloads (default 1080)
- `REQUEST_COOLDOWN_SECONDS` - optional per-user cooldown window in seconds (default 3600); `AUTH_USERS` bypass it
- `MAX_UPLOAD_BYTES` - optional; downloads larger than this are skipped with a friendly message (default 1992294400 ≈ 1.9 GB, the local Bot API server limit; set 52428800 for the cloud Bot API)
- `VERIFY_SSL` - optional; set false only for broken TLS interception (default true)
- `HTTP_PROXY` - optional proxy URL used for direct downloads, yt-dlp, and gallery-dl
- `TELEGRAM_PROXY` - optional proxy URL for the Telegram API connection only; falls back to `HTTP_PROXY` when unset. Ignored when `TELEGRAM_API_URL` points to a self-hosted server (reached directly)
- `TELEGRAM_API_URL` - optional base URL of a self-hosted local Bot API server (e.g. `http://localhost:8081`); raises the bot upload limit from 50 MB to 2000 MB. Run the official `telegram-bot-api` binary with `--local --api-id ... --api-hash ...` (keys from my.telegram.org) next to the bot; leave empty for the cloud Bot API
- `TWITTER_COOKIES_FILE` / `TWITTER_COOKIES` - optional; path to a Netscape-format cookies.txt exported from a logged-in X/Twitter account, or the file content pasted into `.env` as a multiline value; enables downloads of 18+ (NSFW) posts. Export cookies with a browser extension ("Get cookies.txt LOCALLY") while logged in to x.com.

## Project Layout

- root runtime entrypoints: `bot.py`, `app.py`, `config.py`
- routers: `routers/`
- services: `services/`
- shared helpers and models: `utils/`
- tests: `tests/`

## Docker

Build and run the container with your existing `.env` file:

```bash
docker build -t tg-media-downloader .
docker run --env-file .env tg-media-downloader
```

## Checks

Run the same core checks used in GitHub Actions:

```bash
uv run pytest
uv run pylint $(git ls-files '*.py')
```
