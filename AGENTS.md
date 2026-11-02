# Repository Guidelines

## Project Overview

`tg-media-downloader` (package name `all-url-uploader`, v3.0.0) — a self-hosted Telegram bot that takes direct file links, `yt-dlp`-supported URLs, and Twitter/X posts (via `gallery-dl`) and sends the media back to Telegram with correct media type, metadata, and optional per-user thumbnails. Fork of `kalanakt/All-Url-Uploader`, reworked for self-hosting: inline mode, gallery-dl Twitter/X support (incl. NSFW via cookies), split download/Telegram proxies, and local Bot API server support (raises upload limit 50 MB → 2000 MB).

Stack: Python 3.11 (strictly `>=3.11,<3.12`), aiogram 3, yt-dlp and gallery-dl as subprocesses, aiohttp for direct downloads, hachoir for media metadata.

## Architecture & Data Flow

Single-process aiogram polling bot. Entry: `bot.py` → `app.py:run()` → `Settings.from_env()` → `create_dispatcher()` → `start_polling()`. No FSM storage, no webhooks, no database.

Layers and their contracts:

- **`routers/`** — aiogram handlers only. Registered in order `commands → thumbnails → inline → intake → callbacks` (order matters: `intake`'s catch-all `F.text` must come last). Routers never probe, download, or upload; they build a request through `services/requests.py` constructors and hand off to the executor.
- **`services/pipeline.py`** — the probe ladder and downloader selection for every flow. `prepare_request(...)` owns music → gallery → auto-best/YouTube → yt-dlp → direct; `download_artifacts(...)` is the DM dispatch table; `download_inline(...)` re-runs the ladder for inline taps. Status is a parameter; delivery is not.
- **`services/executor.py`** — DM delivery and cache bookkeeping. `execute_request(stored, option, ...)` handles the gallery CDN fast path and the mornye branch, otherwise calls `pipeline.download_artifacts`, then `services/delivery.py`. Inline requests bypass it through `services/inline_flow.py:run_inline_download`.
- **`services/`** — pipeline and flow (`pipeline.py`, `executor.py`, `inline_flow.py`, `requests.py`), downloaders (`ytdlp.py`, `gallery.py`, `direct_downloads.py`, `mornye.py`), delivery (`delivery.py`), status (`status.py`, `progress.py`), classification (`sources.py`, `parsing.py`), and state stores (`request_store.py`, `media_cache.py`, `thumbnail_store.py`, `cooldown.py`).
- **`utils/`** — pure helpers: `models.py` (`RequestType`, per-variant payloads, shared dataclasses), `callbacks.py` (typed `CallbackData`), `keyboards.py`, `text.py` (all user-facing strings), `logging_config.py`.

Main flows:

1. **Private message** (`routers/intake.py:intake_message`): extract URL → cooldown check → media-cache hit? → direct-image CDN fast path → `pipeline.prepare_request` (music | gallery | auto-best/YouTube | yt-dlp selection | direct) → immediate `execute_request` for gallery/music/auto-best, otherwise a format keyboard.
2. **Callback** (`routers/callbacks.py`): load `StoredRequest` by token → `execute_request`.
3. **Inline mode** (`routers/inline.py` + `services/inline_flow.py`): query → cached results / Twitter CDN photo results / tappable download articles (result_id = request token); `run_inline_download` wraps the whole task in `requests.lifecycle`, calls `pipeline.download_inline`, swaps the first artifact into the inline message, and DMs the overflow.
4. **Delivery** (`services/delivery.py`): attach per-user thumbnail → fill missing metadata via `asyncio.to_thread` (hachoir) → `reply_*` or media groups of 10 → delete local file, record file_ids in `MediaCache`.

State is *not* in FSM: pending requests live as JSON files in `DOWNLOADS/requests/` (token = `uuid4().hex[:10]`, swept after 24 h at startup), resolved URL→file_id mappings in `DOWNLOADS/media_cache.json`, thumbnails in `DOWNLOADS/thumbnails/{user_id}.jpg`.

Domain constraints worth knowing before editing:

- Tweet media on the CDN path splits by size: photos always post instantly, videos ≤16 MB post instantly as `InlineQueryResultVideo` (Telegram's fetcher handles `video.twimg.com` since Oct 2026), larger videos become a poster-photo result that the background download edits into the video via `file_id` — an inline message edit cannot upload a new file, and the URL fetcher caps near 20 MB. A poster result is already a media message, so its `StatusSurface` is muted (`media_message=True`): text edits are impossible there.
- Local Bot API server (`TELEGRAM_API_URL`) is always reached directly, bypassing any proxy.
- `text/html` direct downloads are rejected; page URLs route through yt-dlp.
- Keyboards never embed payload data — callbacks carry only a token resolved via `RequestStore`.

## Key Directories

- `routers/` — aiogram handlers: `intake.py` (main text pipeline), `callbacks.py`, `inline.py`, `commands.py`, `thumbnails.py`
- `services/` — business logic: `pipeline.py` (probe ladder + downloader selection), `requests.py` (named constructors + lifecycle), `executor.py` / `inline_flow.py` (flows), `delivery.py` (all Telegram sends), `status.py`, `ytdlp.py` / `gallery.py` / `direct_downloads.py` / `mornye.py` (downloaders), `sources.py`, `request_store.py`, `media_cache.py`, `cooldown.py`, `thumbnail_store.py`, `proc.py` (subprocess runner), `progress.py`, `parsing.py`, `media.py`
- `utils/` — `models.py`, `callbacks.py`, `keyboards.py`, `text.py`, `logging_config.py`
- `tests/` — pytest suite (mirrors services/routers by topic)
- `DOWNLOADS/` — runtime data dir (gitignored): `requests/`, `work/`, `thumbnails/`, `media_cache.json`, `twitter-cookies.txt`

## Development Commands

```bash
cp .env.example .env          # fill BOT_TOKEN, OWNER_ID (required)
uv sync --group dev           # install runtime + dev deps
uv run python bot.py          # run the bot
uv run pytest                 # full test suite (~25s, 185 tests)
uv run pytest tests/test_config.py -k twitter   # subset
uv run pylint $(git ls-files '*.py')   # lint (must score 10/10, fail-under=10.0; skip deleted tracked files)
docker build -t tg-media-downloader . && docker run --env-file .env tg-media-downloader
```

## Code Conventions & Common Patterns

- **Async everywhere**: aiogram 3 handlers; subprocesses via `services/proc.py:run_command` (`asyncio.create_subprocess_exec` in own process group, streamed output, `SIGKILL` on timeout); blocking metadata reads wrapped in `asyncio.to_thread`. No global semaphore/pool — concurrency exists only *within* one direct download (`DOWNLOAD_THREADS`, hard-capped 16) and one task per inline request.
- **DI via workflow_data**: shared singletons injected in `app.py` (`settings`, `cooldown`, `request_store`, `thumbnail_store`, `media_cache`, `mornye`); handlers declare them as typed parameters by name. Add new shared services there, not as new globals.
- **Error handling**: exceptions, not result types. `proc.run_command` raises `RuntimeError`; size violations raise `FileTooLargeError` (`utils/models.py`). User-facing boundary is a broad `except Exception` in `executor.execute_request` / `inline_flow.run_inline_download` that edits the status message with the escaped error. Catch `TelegramAPIError` narrowly only where a fallback exists. Progress reporting must never fail a download (bare `except Exception: pass` with `# pylint: disable=broad-exception-caught`). `probe_gallery` never raises — returns `GalleryProbe(error=...)`.
- **Logging**: stdlib, module-level `logger = logging.getLogger(__name__)`, message style `"Event | key=%s"`. URLs through `safe_url_label()` (strips query), commands through `redact_command()` (masks `--cookies`/`-C`/`--password`/`--proxy` values). Never log raw URLs with credentials or cookie paths.
- **User-facing text**: all strings in `utils/text.py`, HTML parse mode is the bot default — interpolate untrusted values only via `text.esc()`.
- **Naming**: snake_case functions/modules, PascalCase classes, UPPER_CASE consts, `_` prefix for private helpers. Pylint bans `map()` and `input()`.
- **Line length 120** (pylint operative limit; the `autopep8` 150 config in pyproject is unused — autopep8 isn't a dev dep).
- **Cleanup invariant**: every terminal path must delete per-request state and its work directory, and unlink uploaded local files. Use `services/requests.py:lifecycle(store, token)` for flows that own the token (inline does); do not hand-roll `request_store.delete`. Preserve `path.unlink(missing_ok=True)` when adding flows.
- **Request types live in `utils.models.RequestType`** (8 values: `gallery_media`, `mornye`, `youtube_quick`, `ytdlp_selection`, `ytdlp_auto`, `direct_download`, `inline_media`, `inline_gallery`) and are persisted in the legacy `StoredRequest.info` dict. Construct requests only through `services/requests.py`; read per-variant data through `stored.payload` (`GalleryPayload`, `ProbePayload`, `DirectPayload`, …), never by poking `info` keys. Send types stay string literals (`"photo"|"video"|"audio"|"video_note"|"document"|"animation"`). `DownloadOption.mode` is written but never read — dispatch happens on `request_type`.

## Important Files

- `bot.py`, `app.py` — entry point and wiring (session selection: local Bot API / proxy / default; DI of `mornye`)
- `config.py` — `Settings` dataclass + `from_env()`; all env parsing lives here (strict parsers raise `RuntimeError`)
- `services/pipeline.py` — probe ladder (`prepare_request`) and downloader dispatch (`download_artifacts`, `download_inline`)
- `services/requests.py` — named request constructors and the `lifecycle` cleanup context manager
- `services/executor.py` — DM delivery/cache flow around the pipeline
- `services/delivery.py` — all Telegram sends (Telegram Fetch `send_url_media`, Direct Upload, albums of 10)
- `utils/models.py` — `RequestType` + per-variant payloads, `ParsedInput`, `StoredRequest`, `DownloadOption`, `DownloadArtifact`, `CachedMedia`, `FileTooLargeError`
- `utils/callbacks.py` — typed callback prefixes: `ui:`, `req:`, `icxl`, `gnav:`
- `.env.example` — all config vars; `README.md` + `CHANGELOG.md` are the primary docs (Keep a Changelog format, fork changes under `[Unreleased]`). `GLOSSARY.md` holds the domain vocabulary — canonical names for terms, `_Avoid_` lists; read it before naming anything new.

Notable env vars (defaults in `config.py`): `BOT_TOKEN`/`OWNER_ID` required; `TELEGRAM_API_URL` (local Bot API, 2000 MB uploads); `TELEGRAM_PROXY` (falls back to `HTTP_PROXY`); `TWITTER_COOKIES` (Netscape cookies.txt *path or inline content*, used by both yt-dlp and gallery-dl); `DOWNLOAD_THREADS` (16, capped); `MAX_UPLOAD_BYTES` (default ≈1.9 GB; set 52428800 for cloud API); `REQUEST_COOLDOWN_SECONDS` (3600; `AUTH_USERS` bypass). `GALLERY_PROBE_TIMEOUT` (15s) is documented in `.env.example:19`.

## Runtime/Tooling Preferences

- **Package manager: uv only** (`uv sync`, `uv run`). Lockfile `uv.lock` is authoritative (`uv sync --frozen` in Docker).
- **Python 3.11 exactly** (`>=3.11,<3.12` in pyproject; `==3.11.*` in uv.lock). No `.python-version` file — uv auto-resolves.
- `ffmpeg` required on the system for yt-dlp post-processing (installed in the Dockerfile).
- Do not hand-edit `all_url_uploader.egg-info/` (generated).
- Docker image runs as root with no VOLUME; `.env` is passed at runtime (`--env-file`), never baked in.

## Testing & QA

- pytest + pytest-asyncio; `asyncio_mode = "auto"` and `testpaths = ["tests"]` in pyproject. House style still marks every async test with explicit `@pytest.mark.asyncio` — keep it.
- Current baseline: **185 tests, all passing** (`uv run pytest`, ~25 s).
- Shared factories in `tests/conftest.py` (`make_settings`, `make_media_cache`, `make_message`) — imported explicitly via `from tests.conftest import ...` (`tests` is a real package). conftest defines *no* fixtures; per-module `make_<x>_settings(...)` variants are the pattern.
- Mocking: `unittest.mock.AsyncMock` only, patched with `monkeypatch.setattr` at the **consumer module's namespace** as a string (e.g. `monkeypatch.setattr("services.pipeline.download_best_quality", ...)` or `"services.executor.ChatDelivery.deliver"`), never `mock.patch` decorators. Fake Telegram objects are `SimpleNamespace` with AsyncMock methods; domain objects are real `utils.models` dataclasses; filesystem state goes under `tmp_path` (with real magic bytes when the file must parse).
- No network in tests — everything external is patched. Sole exception: `tests/test_proc_progress.py` spawns real `bash`/`echo`/`sleep` (needs POSIX tools on PATH).
- Gotcha: a real `.env` sits at repo root. Any test calling `Settings.from_env()` must replicate `tests/test_config.py`'s autouse fixture (`monkeypatch.chdir(tmp_path)` + `PYTHON_DOTENV_DISABLED=1`) — `find_dotenv()` resolves from `config.py`, so chdir alone is insufficient.
- Expectations: new behavior in `services/` or `routers/` comes with tests following the patterns above; lint must stay at 10/10 (`fail-under=10.0`); regression tests get a short docstring naming the bug they pin.
