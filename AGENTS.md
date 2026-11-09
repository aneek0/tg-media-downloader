# Repository Guidelines

## Project Overview

`tg-media-downloader` (package name `all-url-uploader`, v3.0.0) — a self-hosted Telegram bot that takes direct file links, `yt-dlp`-supported URLs, and Twitter/X posts (via `gallery-dl`) and sends the media back to Telegram with correct media type and metadata. Fork of `kalanakt/All-Url-Uploader`, reworked for self-hosting: inline mode, gallery-dl Twitter/X support (incl. NSFW via cookies), split download/Telegram proxies, and local Bot API server support (raises upload limit 50 MB → 2000 MB).

Stack: Python 3.11 (strictly `>=3.11,<3.12`), aiogram 3, yt-dlp and gallery-dl as subprocesses, aiohttp for direct downloads, hachoir for media metadata.

## Architecture & Data Flow

Single-process aiogram polling bot. Entry: `bot.py` → `app.py:run()` → `Settings.from_env()` → `create_dispatcher()` → `start_polling()`. No FSM storage, no webhooks, no database.

Layers and their contracts:

- **`routers/`** — aiogram handlers only. Registered in order `commands → settings → follows → catalogue → inline → intake → callbacks` (order matters: `intake`'s catch-all `F.text` must come after the other message handlers). Routers never probe, download, or upload; they open a Request through `session.request_store.open(...)` and hand off to the executor, inline flow or catalogue presenter. Generic `catalogue` inline handlers MUST return aiogram `UNHANDLED` for URL queries and non-Artist-Card chosen results so the later `inline` router can process them; a bare `return` marks the event handled and leaves the message at `Processing your link…` forever.
- **`services/pipeline.py`** — the probe ladder and the one downloader table for every flow. `prepare_request(session, parsed)` owns music → gallery → auto-best/YouTube → yt-dlp → direct and persists the probe result in the payload. `download_artifacts(session, stored, choice, *, status)` is the only dispatch table: gallery, mornye, direct, YouTube and yt-dlp rows; it returns `Downloaded` (artifacts plus the resolved music target and cover, which delivery still needs), so no caller depends on row order. `DownloadChoice` (Option + audio tier) is the single "how to download" contract for DM and inline. Status is a parameter; delivery is not.
- **`services/executor.py`** — DM delivery and cache bookkeeping. `execute_request(...)` handles the gallery CDN fast path, calls `pipeline.download_artifacts` once, then `services/delivery.py`; the music finish (cover photo, per-file uploads, partial-failure report, single-track cache) runs when the download reports a music target. Inline requests bypass it through `services/inline_flow.py:run_inline_download(session, ...)`.
- **`services/`** — session (`session.py`: the `Session` aggregate — settings, cooldown, request/media stores, catalogue search, user settings, follows, mornye), pipeline and flow (`pipeline.py`, `executor.py`, `inline_flow.py`, `catalogue_flow.py`, `pixiv_flow.py`), downloaders (`ytdlp.py`, `gallery.py`, `direct_downloads.py`, `mornye.py`), providers (`pixiv.py`: URL kinds, profile index, gallery-dl config), delivery (`delivery.py`: the Send Type table, cached resends, cached inline results), status (`status.py`, `progress.py`), classification (`sources.py`, `parsing.py`), and state stores (`request_store.py`, `json_store.py`, `media_cache.py`, `user_settings.py`, `follow_store.py`, `cooldown.py`).
- **`utils/`** — pure helpers: `models.py` (`RequestType`, per-variant payloads, shared dataclasses), `callbacks.py` (typed `CallbackData`), `keyboards.py`, `text.py` (all user-facing strings), `logging_config.py`.

Main flows:

1. **Private message** (`routers/intake.py:intake_message`): extract URL → cooldown check → media-cache hit? → direct-image CDN fast path → `pipeline.prepare_request` (music | gallery | auto-best/YouTube | yt-dlp selection | direct) → immediate `execute_request` for gallery/music/auto-best, otherwise a format keyboard.
2. **Callback** (`routers/callbacks.py`): load `StoredRequest` by token → `execute_request`.
3. **Inline mode** (`routers/inline.py` + `services/inline_flow.py`): query → cached results / Twitter CDN photo results / tappable download articles (result_id = request token). The query persists the plan it already made (`InlineMediaPayload.plan`, the gallery probe result) in the Request, so `run_inline_download` reuses a singleton cached audio `file_id` and otherwise runs the same `pipeline.download_artifacts` table as the DM path, swaps the first artifact into the inline message, and DMs the overflow. BotFather `/setinlinefeedback` MUST be **Enabled**, otherwise Telegram never emits `chosen_inline_result` and tappable articles stay at `Processing your link…` without reaching the bot.
4. **Delivery** (`services/delivery.py`): one Send Type table builds both the `InputMedia*` entry (media group, inline edit) and the single `send_*` call, with the media source as a parameter — a local `FSInputFile` or an existing `file_id`; video/audio facts are read once by `media_facts` before the file is unlinked. Cached resends and cached inline results live here too, so `MediaCache` stays a store. Local files are deleted after the upload, `file_id`s recorded in `MediaCache`.

State is *not* in FSM: pending requests live as JSON files in `DOWNLOADS/requests/` (token = `uuid4().hex[:10]`, swept after 24 h at startup), resolved URL→file_id mappings in `DOWNLOADS/media_cache.json`.

Domain constraints worth knowing before editing:

- Tweet media on the CDN path splits by size: photos always post instantly, videos ≤16 MB post instantly as `InlineQueryResultVideo` (Telegram's fetcher handles `video.twimg.com` since Oct 2026), larger videos become a poster-photo result that the background download edits into the video via `file_id` — an inline message edit cannot upload a new file, and the URL fetcher caps near 20 MB. A poster result is already a media message, so its `StatusSurface` is muted (`media_message=True`): text edits are impossible there.
- Local Bot API server (`TELEGRAM_API_URL`) is always reached directly, bypassing any proxy.
- `text/html` direct downloads are rejected; page URLs route through yt-dlp.
- Keyboards never embed payload data — callbacks carry only a token resolved via `RequestStore`.
- Pixiv is a Source Domain with two behaviours: an artwork URL is an ordinary gallery request, while an artist profile becomes a `pixiv_profile` browser. Profiles are paged through Pixiv's OAuth App API (`/v1/user/illusts` takes an `offset`), because gallery-dl cannot seek into a profile (`--post-range` re-scans from the newest work) and the anonymous web endpoints hide logged-in-only works (`/ajax/user/<id>/profile/all` returned 0 works for a 17-work artist). A Profile Page holds whole Works only (≤10 works, ≤50 files) and rejects an oversized single Work instead of splitting it.
- `i.pximg.net` answers 403 without `Referer: https://www.pixiv.net/`, so Telegram cannot fetch Pixiv media and inline Pixiv results are tap-to-download articles, not photo results. The gallery direct-download path adds that Referer, and covers are downloaded by the bot before a Cover Album.
- Pixiv credentials come from gallery-dl itself: `gallery-dl oauth:pixiv` caches the refresh token in `$XDG_CACHE_HOME/gallery-dl/cache.sqlite3` under `gallery_dl.extractor.pixiv._refresh_token_cache`, and the extractor reads it when `refresh-token` is unset. `pixiv.credentials_available()` gates artwork links on that cache plus `PIXIV_REFRESH_TOKEN`; the profile browser exchanges that refresh token for a one-hour access token and caches it in-process, so both surfaces need a login. A token from the environment is written to a temporary 0600 config passed via `--config`; never pass it with `-o`, which `redact_command()` does not mask.

## Key Directories

- `routers/` — aiogram handlers: `intake.py` (main text pipeline), `callbacks.py`, `inline.py`, `catalogue.py`, `settings.py`, `follows.py`, `commands.py`
- `services/` — business logic: `session.py` (`Session` + `build_session`), `pipeline.py`, `executor.py` / `inline_flow.py` / `catalogue_flow.py` / `pixiv_flow.py`, `delivery.py`, `status.py`, `catalogue_search.py`, `follows.py`, `ytdlp.py` / `gallery.py` / `direct_downloads.py` / `mornye.py` / `pixiv.py`, `sources.py`, `request_store.py`, `json_store.py`, `media_cache.py`, `user_settings.py`, `follow_store.py`, `cooldown.py`, `proc.py`, `progress.py`, `parsing.py`, `media.py`
- `utils/` — `models.py`, `callbacks.py`, `keyboards.py`, `text.py`, `logging_config.py`
- `tests/` — pytest suite (mirrors services/routers by topic)
- `DOWNLOADS/` — runtime data dir (gitignored): `requests/`, `work/`, `media_cache.json`, `user_settings.json`, `follows.json`, `twitter-cookies.txt`, `pixiv-cookies.txt`

## Development Commands

```bash
cp .env.example .env          # fill BOT_TOKEN, OWNER_ID (required)
uv sync --group dev           # install runtime + dev deps
uv run python bot.py          # run the bot
uv run pytest                 # full test suite (~21s, 436 tests)
uv run pytest tests/test_config.py -k twitter   # subset
uv run ruff check .             # lint
uv run ty check              # production type checking
docker build -t tg-media-downloader . && docker run --env-file .env tg-media-downloader
```

## Code Conventions & Common Patterns

- **Async everywhere**: aiogram 3 handlers; subprocesses via `services/proc.py:run_command` (`asyncio.create_subprocess_exec` in own process group, streamed output, `SIGKILL` on timeout); blocking metadata reads wrapped in `asyncio.to_thread`. Gallery albums use a shared six-slot `asyncio.Semaphore` with three downloads per album; direct downloads use `DOWNLOAD_THREADS` (hard-capped 16), and inline requests use one task each.
- **DI via one `session`**: `app.py` builds `services/session.py:Session` (`settings`, `cooldown`, `request_store`, `media_cache`, `mornye`) and injects it as `workflow_data["session"]`; handlers and flows declare `session: Session` as a single parameter and read what they need off it. A new shared service becomes a `Session` field, not a new global or a new handler parameter. Per-user preferences are read through `session.preferences(user_id)` and written through `session.update_preferences(user_id, ...)` — both seed the bot's own settings as defaults, so routers and flows never call `session.user_settings.get()`/`.update()` themselves. Downloaded audio is cached per tier (`mornye.audio_cache_key` appends `#hires`), so a CD request is never answered with a 24-bit file.
- **Error handling**: exceptions, not result types. `proc.run_command` raises `RuntimeError`; size violations raise `FileTooLargeError` (`utils/models.py`). User-facing boundary is a broad `except Exception` in `executor.execute_request` / `inline_flow.run_inline_download` that edits the status message with the escaped error. Catch `TelegramAPIError` narrowly only where a fallback exists. Progress reporting must never fail a download (`contextlib.suppress(Exception)` around status edits). `probe_gallery` never raises — returns `GalleryProbe(error=...)`.
- **Logging**: stdlib, module-level `logger = logging.getLogger(__name__)`, message style `"Event | key=%s"`. URLs through `safe_url_label()` (strips query), commands through `redact_command()` (masks `--cookies`/`-C`/`--password`/`--proxy` values). Never log raw URLs with credentials or cookie paths.
- **User-facing text**: all strings in `utils/text.py`, HTML parse mode is the bot default — interpolate untrusted values only via `text.esc()`.
- **Naming**: snake_case functions/modules, PascalCase classes, UPPER_CASE consts, `_` prefix for private helpers. Avoid `map()` and `input()`.
- **Line length 120** (Ruff `E501`; the `autopep8` 150 config in pyproject is unused — autopep8 isn't a dev dep).
- **Cleanup invariant**: every terminal path must delete per-request state and its work directory, and unlink uploaded local files. Use `session.request_store.lifecycle(token)` for flows that own the token (inline does); do not hand-roll `request_store.delete`. Preserve `path.unlink(missing_ok=True)` when adding flows.
- **Request types live in `utils.models.RequestType`** (10 values: `gallery_media`, `mornye`, `youtube_quick`, `ytdlp_selection`, `ytdlp_auto`, `direct_download`, `inline_media`, `inline_gallery`, `catalogue_browse`, `pixiv_profile`) and are persisted in the legacy `StoredRequest.info` dict. Open requests only through `session.request_store.open(type, parsed, payload=...)`; it rejects a payload shape that does not belong to the type, and `update_payload(...)` is the only way to refresh state under an existing token (Artist Cards, Pixiv pages). Read per-variant data through `stored.payload` (`GalleryPayload`, `ProbePayload`, `DirectPayload`, `CataloguePayload`, `PixivProfilePayload`, …), never by poking `info` keys. Send types stay string literals (`"photo"|"video"|"audio"|"video_note"|"document"|"animation"`). `DownloadOption.mode` is written but never read — dispatch happens on `request_type`.

## Important Files

- `bot.py`, `app.py` — entry point and wiring (session selection: local Bot API / proxy / default; builds the `Session`, injects it into `workflow_data`)
- `services/session.py` — `Session` (frozen dataclass) plus `build_session(settings, mornye=None)`; the single dependency handlers and flows receive
- `config.py` — `Settings` dataclass + `from_env()`; all env parsing lives here (strict parsers raise `RuntimeError`)
- `services/pipeline.py` — probe ladder (`prepare_request`) and the single downloader table (`download_artifacts`, `DownloadChoice`, `Downloaded`)
- `services/catalogue_search.py` / `catalogue_flow.py` / `follows.py` — provider search, Artist Card/album UI, and per-user six-hour release polling
- `services/request_store.py` — Request persistence and the lifecycle verbs (`open`, `update_payload`, `lifecycle`); `services/json_store.py` — the one atomic JSON read/write every store uses
- `services/executor.py` — DM delivery/cache flow around the pipeline
- `services/delivery.py` — all Telegram sends (Telegram Fetch `send_url_media`, Direct Upload, media groups of 10, cached resends, cached inline results) and the single Send Type table (`media_facts`, `media_item`)
- `utils/models.py` — `RequestType` + per-variant payloads, `ParsedInput`, `StoredRequest`, `DownloadOption`, `DownloadArtifact`, `CachedMedia`, `FileTooLargeError`
- `utils/callbacks.py` — typed callback prefixes: `ui:`, `req:`, `icxl`, `gnav:`, `cat:`, `fol:`, `set:`
- `.env.example` — all config vars; `README.md` + `CHANGELOG.md` are the primary docs (Keep a Changelog format, fork changes under `[Unreleased]`). `GLOSSARY.md` holds the domain vocabulary — canonical names for terms, `_Avoid_` lists; read it before naming anything new.

Notable env vars (defaults in `config.py`): `BOT_TOKEN`/`OWNER_ID` required; `TELEGRAM_API_URL` (local Bot API, 2000 MB uploads); `TELEGRAM_PROXY` (falls back to `HTTP_PROXY`); `TWITTER_COOKIES` (Netscape cookies.txt *path or inline content*, used by both yt-dlp and gallery-dl); `DOWNLOAD_THREADS` (16, capped); `MAX_UPLOAD_BYTES` (default ≈1.9 GB; set 52428800 for cloud API); `REQUEST_COOLDOWN_SECONDS` (3; `AUTH_USERS` bypass); `SEARCH_PROVIDER` (`deezer`, `spotify-web`, or `tidal-web`); `FOLLOW_POLL_SECONDS` (21600, six hours); `MORNYE_SERVICES` (required for catalogue audio); `MORNYE_ENRICH_EXTENSIONS=0` disables optional per-extension metadata fan-out and its extra Turnstile challenges. `GALLERY_PROBE_TIMEOUT` defaults to 15s. `MORNYE_EXT_ALLOW_PRIVATE_NETWORK=1` is required behind sing-box FakeIP/TUN so extension search and downloads are not blocked by netguard; enable only for trusted extensions.

## Runtime/Tooling Preferences

- **Package manager: uv only** (`uv sync`, `uv run`). Lockfile `uv.lock` is authoritative (`uv sync --frozen` in Docker).
- **Python 3.11 exactly** (`>=3.11,<3.12` in pyproject; `==3.11.*` in uv.lock). No `.python-version` file — uv auto-resolves.
- `ffmpeg` required on the system for yt-dlp post-processing and MP3 catalogue downloads (installed in the Dockerfile).
- Do not hand-edit `all_url_uploader.egg-info/` (generated).
- Docker image runs as root with no VOLUME; `.env` is passed at runtime (`--env-file`), never baked in.

## Testing & QA

- pytest + pytest-asyncio; `asyncio_mode = "auto"` and `testpaths = ["tests"]` in pyproject. House style still marks every async test with explicit `@pytest.mark.asyncio` — keep it.
- Current baseline: **324 tests, all passing** (`uv run pytest`, ~14 s).
- Shared factories in `tests/conftest.py` (`make_settings`, `make_media_cache`, `make_message`) — imported explicitly via `from tests.conftest import ...` (`tests` is a real package). conftest defines *no* fixtures; per-module `make_<x>_settings(...)` variants are the pattern.
- Mocking: `unittest.mock.AsyncMock` only, patched with `monkeypatch.setattr` at the **consumer module's namespace** as a string (e.g. `monkeypatch.setattr("services.pipeline.download_best_quality", ...)` or `"services.executor.ChatDelivery.deliver"`), never `mock.patch` decorators. Fake Telegram objects are `SimpleNamespace` with AsyncMock methods; domain objects are real `utils.models` dataclasses; filesystem state goes under `tmp_path` (with real magic bytes when the file must parse).
- No network in tests — everything external is patched. Sole exception: `tests/test_proc_progress.py` spawns real `bash`/`echo`/`sleep` (needs POSIX tools on PATH).
- Gotcha: a real `.env` sits at repo root. Any test calling `Settings.from_env()` must replicate `tests/test_config.py`'s autouse fixture (`monkeypatch.chdir(tmp_path)` + `PYTHON_DOTENV_DISABLED=1`) — `find_dotenv()` resolves from `config.py`, so chdir alone is insufficient.
- Expectations: new behavior in `services/` or `routers/` comes with tests following the patterns above; `uv run ruff check .` and `uv run ty check` must pass; regression tests get a short docstring naming the bug they pin.
