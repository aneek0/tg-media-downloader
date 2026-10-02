# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

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

### Changed

- Forked from [kalanakt/All-Url-Uploader](https://github.com/kalanakt/All-Url-Uploader); upstream
  docs site removed in favor of this file.
- Single executor flow for request execution (one place instead of per-router ad-hoc logic).
- Hermetic config tests via `PYTHON_DOTENV_DISABLED`.
- Parallel direct-download segments retry in place up to 3 times, resuming from the last
  written byte, before the whole file falls back to a single-stream restart.

### Removed

- `docs/` external documentation site (Next.js/Nextra), `.hintrc`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, GitHub Actions workflows (`.github/`), Renovate config,
  Heroku `app.json`, Fiverr banner, upstream deploy buttons, contributor table workflow,
  FUNDING.yml.

## [3.0.0] and earlier

See the upstream repository history:
<https://github.com/kalanakt/All-Url-Uploader/commits/main>
