"""Pixiv: URL classification, OAuth App API client, gallery-dl credentials.

Artwork pages are downloaded by gallery-dl like any other gallery. Artist
profiles are paged here instead: gallery-dl cannot seek into a profile
(``--post-range`` re-scans from the newest work), while Pixiv's own App API
takes an offset. The API call needs the same OAuth refresh token gallery-dl
uses, which comes from ``PIXIV_REFRESH_TOKEN`` or gallery-dl's own cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pickle
import re
import sqlite3
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import aiohttp

from config import Settings

logger = logging.getLogger(__name__)

PIXIV_ROOT = "https://www.pixiv.net"
PIXIV_HOSTS = frozenset({"pixiv.net", "www.pixiv.net", "pixiv.me", "i.pximg.net"})
PIXIV_IMAGE_HOSTS = frozenset({"i.pximg.net", "i.pixiv.net"})

APP_API = "https://app-api.pixiv.net"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
# Public mobile-app client credentials; gallery-dl ships the same pair.
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"

# One Profile Page is at most this many Works and this many files.
PIXIV_PAGE_WORKS = 10
PIXIV_PAGE_FILES = 50
PIXIV_COVER_DOWNLOADS = 5

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124 Safari/537.36"
)
_APP_HEADERS = {
    "App-OS": "ios",
    "App-OS-Version": "16.7.2",
    "App-Version": "7.19.1",
    "User-Agent": "PixivIOSApp/7.19.1 (iOS 16.7.2; iPhone12,8)",
    "Referer": APP_API + "/",
}
_WORK_PATH = re.compile(r"^/(?:en/)?(?:artworks|i)/(\d+)(?:/|$)")
_UNLISTED_PATH = re.compile(r"^/(?:en/)?artworks/unlisted/([A-Za-z0-9_-]+)/?$")
_PROFILE_PATH = re.compile(
    r"^/(?:en/)?(?:users|u)/(\d+)(?:/(?:artworks|illustrations|manga))?/?$"
)
_PIXIV_ME_PATH = re.compile(r"^/([A-Za-z0-9_-]+)/?$")

# One exchange per process is enough: access tokens live an hour.
_access: dict[str, tuple[str, float]] = {}


class PixivError(RuntimeError):
    """Pixiv lookup or download failure, safe to show to the user."""


@dataclass(frozen=True, slots=True)
class PixivWork:
    work_id: str
    title: str
    artist: str
    pages: int
    cover_url: str
    ugoira: bool

    @property
    def url(self) -> str:
        return work_url(self.work_id)


def work_url(work_id: str) -> str:
    return f"{PIXIV_ROOT}/artworks/{work_id}"


def is_pixiv_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in PIXIV_HOSTS


def is_pixiv_image_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in PIXIV_IMAGE_HOSTS


def pixiv_kind(url: str) -> str | None:
    """``work`` | ``profile`` | ``unsupported`` for a Pixiv URL, else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in PIXIV_HOSTS:
        return None
    if host == "pixiv.me":
        return "profile" if _PIXIV_ME_PATH.match(parsed.path) else "unsupported"
    if is_pixiv_image_url(url):
        # gallery-dl resolves a direct image URL back to its whole Work.
        return "work"
    path = parsed.path
    if _WORK_PATH.match(path) or _UNLISTED_PATH.match(path):
        return "work"
    if _PROFILE_PATH.match(path):
        return "profile"
    query = parse_qs(parsed.query)
    if query.get("illust_id"):
        return "work"
    if path.endswith("/member.php") and query.get("id"):
        return "profile"
    return "unsupported"


def profile_user_id(url: str) -> str | None:
    match = _PROFILE_PATH.match(urlparse(url).path)
    if match:
        return match.group(1)
    query = parse_qs(urlparse(url).query)
    for key in ("id", "user_id"):
        values = query.get(key)
        if values and values[0].isdigit():
            return values[0]
    return None


def work_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = _WORK_PATH.match(parsed.path)
    if match:
        return match.group(1)
    values = parse_qs(parsed.query).get("illust_id")
    if values and values[0].isdigit():
        return values[0]
    return None


def _cookie_header(settings: Settings) -> str:
    """Cookie header parsed from the Netscape file, for web requests."""
    path = cookies_file(settings)
    if path is None:
        return ""
    pairs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) >= 7 and "pixiv" in fields[0]:
            pairs.append(f"{fields[5]}={fields[6]}")
    return "; ".join(pairs)


def _image_headers(settings: Settings) -> dict[str, str]:
    """i.pximg.net answers 403 unless the request carries a Pixiv Referer."""
    headers = {"Referer": PIXIV_ROOT + "/", "User-Agent": _UA}
    cookies = _cookie_header(settings)
    if cookies:
        headers["Cookie"] = cookies
    return headers


def _connector(settings: Settings) -> aiohttp.TCPConnector:
    if settings.verify_ssl:
        return aiohttp.TCPConnector()
    return aiohttp.TCPConnector(ssl=False)


# --- credentials ---------------------------------------------------------


def cookies_file(settings: Settings) -> Path | None:
    """Netscape cookies.txt for gallery-dl's Pixiv extractor."""
    raw = settings.pixiv_cookies
    if not raw:
        return None
    if "\n" in raw or "\t" in raw:
        path = settings.download_location / "pixiv-cookies.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return path.resolve()
    candidate = Path(raw)
    if candidate.is_file():
        return candidate.resolve()
    logger.warning("Pixiv cookies file not found | path=%s", candidate)
    return None


_CACHE_TOKEN_KEY = "gallery_dl.extractor.pixiv._refresh_token_cache"


def gallery_cache_path() -> Path:
    """gallery-dl's own cache database (where ``oauth:pixiv`` stores the
    refresh token)."""
    base = os.environ.get("XDG_CACHE_HOME", "~/.cache")
    return Path(os.path.expanduser(base)) / "gallery-dl" / "cache.sqlite3"


def _cached_refresh_token(path: Path | None = None) -> str | None:
    database = path or gallery_cache_path()
    if not database.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute(
                "SELECT value FROM data WHERE key LIKE ? LIMIT 1",
                (f"{_CACHE_TOKEN_KEY}%",),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        token = pickle.loads(row[0])
    except (pickle.UnpicklingError, EOFError, TypeError):
        return None
    return token if isinstance(token, str) and token else None


def has_cached_token(path: Path | None = None) -> bool:
    """True when ``gallery-dl oauth:pixiv`` already cached a refresh token."""
    return _cached_refresh_token(path) is not None


def credentials_available(settings: Settings) -> bool:
    """Pixiv needs a login; it comes from PIXIV_REFRESH_TOKEN or gallery-dl's
    own OAuth cache."""
    return bool(settings.pixiv_refresh_token) or has_cached_token()


def refresh_token(settings: Settings) -> str:
    token = settings.pixiv_refresh_token or _cached_refresh_token()
    if not token:
        raise PixivError(
            "Pixiv needs a login. Run `gallery-dl oauth:pixiv` on the bot host "
            "or set PIXIV_REFRESH_TOKEN."
        )
    return token


async def _access_token(settings: Settings, client: aiohttp.ClientSession) -> str:
    """Exchange the cached refresh token for a one-hour access token."""
    token = refresh_token(settings)
    cached = _access.get(token)
    if cached and cached[1] > time.time():
        return cached[0]
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": token,
        "include_policy": "true",
        "get_secure_url": "1",
    }
    async with client.post(TOKEN_URL, data=data) as response:
        payload = await response.json(content_type=None)
    raw_body = payload.get("response") if isinstance(payload, dict) else None
    response_body: dict = raw_body if isinstance(raw_body, dict) else {}
    access = response_body.get("access_token")
    if not access:
        detail = str(payload)[:200] if payload is not None else "no response"
        raise PixivError(
            "Pixiv rejected the login. Re-run `gallery-dl oauth:pixiv` "
            f"({detail})."
        )
    expires_in = int(response_body.get("expires_in") or 3600)
    _access[token] = (access, time.time() + expires_in - 300)
    logger.info("Pixiv access token refreshed | expires_in=%s", expires_in)
    return access


# --- App API -------------------------------------------------------------


async def _app_get(
    settings: Settings,
    client: aiohttp.ClientSession,
    path: str,
    params: dict[str, str | int],
) -> dict:
    access = await _access_token(settings, client)
    headers = {**_APP_HEADERS, "Authorization": f"Bearer {access}"}
    async with client.get(
        APP_API + path,
        params=params,
        headers=headers,
        proxy=settings.http_proxy or None,
    ) as response:
        if response.status in {401, 403}:
            _access.pop(settings.pixiv_refresh_token, None)
            raise PixivError(
                "Pixiv rejected the login. Re-run `gallery-dl oauth:pixiv`."
            )
        payload = await response.json(content_type=None)
    if not isinstance(payload, dict) or payload.get("error"):
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise PixivError(f"Pixiv: {message or 'request failed'}")
    return payload


# One session per proxy setting for the whole process: a fresh TLS handshake
# costs 0.3-1.3 s here, and every page would otherwise pay for two of them.
_shared: dict[str, aiohttp.ClientSession] = {}


def _client(settings: Settings) -> aiohttp.ClientSession:
    proxy = settings.http_proxy or ""
    session = _shared.get(proxy)
    if session is None or getattr(session, "closed", False):
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.process_max_timeout),
            connector=_connector(settings),
        )
        _shared[proxy] = session
    return session


async def user_works(
    settings: Settings, user_id: str, offset: int, limit: int = PIXIV_PAGE_WORKS
) -> tuple[list[PixivWork], int | None]:
    """One window of an artist's works, plus the next offset (None at the end)."""
    client = _client(settings)
    payload = await _app_get(
        settings,
        client,
        "/v1/user/illusts",
        {
            "user_id": user_id,
            "type": "illust",
            "offset": offset,
            "limit": limit,
            "filter": "for_ios",
        },
    )
    works = [
        work
        for work in (_work_from_json(item) for item in payload.get("illusts") or [])
        if work is not None
    ]
    next_offset = offset + len(works) if payload.get("next_url") else None
    logger.info(
        "Pixiv user works | user=%s offset=%s works=%s more=%s",
        user_id,
        offset,
        len(works),
        next_offset is not None,
    )
    return works, next_offset


async def work(settings: Settings, identifier: str) -> PixivWork:
    """Metadata for one Work, by id."""
    payload = await _app_get(
        settings, _client(settings), "/v1/illust/detail", {"illust_id": identifier}
    )
    parsed = _work_from_json(payload.get("illust"))
    if parsed is None:
        raise PixivError("Pixiv did not return this work.")
    return parsed


def _work_from_json(item: object) -> PixivWork | None:
    if not isinstance(item, dict):
        return None
    work_id = str(item.get("id") or "")
    if not work_id.isdigit():
        return None
    raw_urls = item.get("image_urls")
    urls: dict = raw_urls if isinstance(raw_urls, dict) else {}
    raw_user = item.get("user")
    user: dict = raw_user if isinstance(raw_user, dict) else {}
    kind = str(item.get("type") or "")
    return PixivWork(
        work_id=work_id,
        title=str(item.get("title") or "").strip(),
        artist=str(user.get("name") or "").strip(),
        pages=max(int(item.get("page_count") or 1), 1),
        cover_url=str(urls.get("square_medium") or urls.get("medium") or ""),
        ugoira=kind == "ugoira",
    )


def page_works(works: list[PixivWork]) -> list[PixivWork]:
    """Whole Works only: at most PIXIV_PAGE_WORKS Works and PIXIV_PAGE_FILES
    files. A single Work over the file limit is returned alone so the caller
    can reject it instead of splitting it."""
    taken: list[PixivWork] = []
    total = 0
    for work_item in works[:PIXIV_PAGE_WORKS]:
        if taken and total + work_item.pages > PIXIV_PAGE_FILES:
            break
        taken.append(work_item)
        total += work_item.pages
    return taken


def page_file_count(works: list[PixivWork]) -> int:
    return sum(work_item.pages for work_item in works)


async def resolve_profile_user_id(url: str, settings: Settings) -> str:
    """User id for a profile URL; resolves ``pixiv.me/<name>`` by redirect."""
    known = profile_user_id(url)
    if known:
        return known
    if (urlparse(url).hostname or "").lower() != "pixiv.me":
        raise PixivError("This Pixiv link is not a user profile.")
    async with _client(settings).get(
        url, headers=_image_headers(settings), allow_redirects=True
    ) as response:
        final = str(response.url)
    resolved = profile_user_id(final)
    if not resolved:
        raise PixivError("Could not resolve this Pixiv profile link.")
    return resolved


# --- covers and downloads -------------------------------------------------


async def download_covers(
    settings: Settings, works: list[PixivWork], directory: Path
) -> list[Path]:
    """Fetch Work covers locally: Telegram cannot fetch i.pximg.net itself."""
    directory.mkdir(parents=True, exist_ok=True)
    headers = _image_headers(settings)
    limiter = asyncio.Semaphore(PIXIV_COVER_DOWNLOADS)

    async def cover(
        client: aiohttp.ClientSession, item: PixivWork
    ) -> Path | None:
        if not item.cover_url:
            return None
        # Keyed by Work so a page the user returns to reuses the same files;
        # the request's work directory removes them when browsing ends.
        target = directory / f"{item.work_id}.jpg"
        if target.exists() and target.stat().st_size:
            return target
        async with limiter:
            try:
                async with client.get(
                    item.cover_url,
                    headers=headers,
                    proxy=settings.http_proxy or None,
                ) as response:
                    response.raise_for_status()
                    partial = target.with_name(f"{target.name}.part")
                    partial.write_bytes(await response.read())
                    partial.replace(target)
            except (aiohttp.ClientError, OSError) as exc:
                logger.info(
                    "Pixiv cover download failed | work=%s error=%s", item.work_id, exc
                )
                return None
        return target

    paths = await asyncio.gather(*(cover(_client(settings), item) for item in works))
    return [path for path in paths if path is not None]


@contextmanager
def gallery_config(settings: Settings) -> Generator[list[str], None, None]:
    """Extra gallery-dl flags carrying the Pixiv OAuth token.

    Only used for PIXIV_REFRESH_TOKEN: the token must not reach argv (it is
    logged and visible in the process list), so it travels in a 0600 config
    file removed after the run. Without it gallery-dl uses its own cache.
    """
    if not settings.pixiv_refresh_token:
        yield []
        return
    fd, raw_path = tempfile.mkstemp(prefix="pixiv-", suffix=".json")
    path = Path(raw_path)
    try:
        os.fchmod(fd, 0o600)
        config = {
            "extractor": {
                "pixiv": {"refresh-token": settings.pixiv_refresh_token},
            }
        }
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        yield ["--config", raw_path]
    finally:
        path.unlink(missing_ok=True)


def friendly_error(exc: Exception) -> str:
    """Turn a gallery-dl/Pixiv failure into something the user can act on."""
    message = str(exc)
    lowered = message.lower()
    if "refresh-token" in lowered or "authentication" in lowered:
        return (
            "Pixiv needs a login. Run `gallery-dl oauth:pixiv` on the bot "
            "host (gallery-dl then uses the cached token) or set "
            "PIXIV_REFRESH_TOKEN."
        )
    return message