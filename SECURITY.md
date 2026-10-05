# Security Policy

## Supported versions

Only the latest `main` branch and the latest tagged release are supported.

## Reporting a vulnerability

Open a private security advisory instead of a public issue:

1. GitHub → **Security** → **Report a vulnerability**,
   or email the repository owner.

Include: affected version/commit, reproduction steps, impact assessment.

## Sensitive data this bot handles

- `BOT_TOKEN` — full control of the bot account. Store it in `.env`
  (gitignored) or a secret manager; never commit it.
- `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` (local Bot API server deployment)
  and `TWITTER_COOKIES(_FILE)` — a Netscape cookies.txt export of a logged-in
  X/Twitter session. It is equivalent to that account's session: keep the file
  permissions restrictive (`chmod 600`), and if it leaks, log out of the
  session on x.com (Settings → Security → Sessions) to invalidate it.
- Downloaded media and the media cache live under `DOWNLOADS/` — on shared
  hosts, make sure the directory is not world-readable.

## Dependency freshness

The bot shells out to `yt-dlp` and `gallery-dl`, whose extractors break or
need updates as sites change. This repo has no embargoed-security-fix process:
if you run this in production, keep those tools (and the Docker image) current
and subscribe to their releases.

## Scope

- This fork does not accept security reports for upstream `kalanakt/All-Url-Uploader` code predating the fork.
- Docker images built by this repo include `curl`, `ffmpeg`, and `aria2` from
  Debian repositories; report CVEs in those packages to their maintainers.
