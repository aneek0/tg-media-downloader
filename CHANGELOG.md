# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Changed

- Forked from [kalanakt/All-Url-Uploader](https://github.com/kalanakt/All-Url-Uploader); upstream
  docs site removed in favor of this file.
- Single executor flow for request execution (one place instead of per-router ad-hoc logic).
- Hermetic config tests via `PYTHON_DOTENV_DISABLED`.

### Removed

- `docs/` external documentation site (Next.js/Nextra), `.hintrc`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, Fiverr banner, upstream deploy buttons, contributor table workflow,
  FUNDING.yml.

## [3.0.0] and earlier

See the upstream repository history:
<https://github.com/kalanakt/All-Url-Uploader/commits/main>
