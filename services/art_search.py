"""Art text search: Pixiv and Pawchive through the same inline surface.

Both providers answer the same two questions the music catalogues do:
"who is this artist" and "what is this work". Their results are ordinary
gallery requests — an artwork URL for Pixiv, a post URL for Pawchive — so
download goes through the pipeline's gallery row, never a new downloader.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import aiohttp

from config import Settings
from services import pixiv

logger = logging.getLogger(__name__)


class PawchiveError(RuntimeError):
    """A Pawchive lookup or download failure, safe to show to the user."""

#: Telegram caps an inline answer at 50 results; leave room for the pager.
_RESULT_LIMIT = 8

#: Pawchive's creator index is one 16 MB list; one copy per process,
#: held in a one-slot dict so the async fill needs no ``global``.
_creators: dict[str, list[dict]] = {}
_creators_lock = asyncio.Lock()


@dataclass(slots=True)
class ArtHit:
    """One search hit: a creator profile or a single work/post."""

    title: str
    description: str
    external_url: str
    thumbnail_url: str | None = None


async def search_pixiv(settings: Settings, query: str) -> list[ArtHit]:
    """Artists by name, then works by title, from the App API search."""
    if not pixiv.credentials_available(settings):
        raise pixiv.PixivError(
            "Pixiv needs a login for search. Re-run `gallery-dl oauth:pixiv`."
        )
    client = pixiv._client(settings)
    payload = await pixiv._app_get(
        settings,
        client,
        "/v1/search/user",
        {"word": query, "limit": _RESULT_LIMIT},
    )
    hits: list[ArtHit] = []
    for preview in payload.get("user_previews") or []:
        user = preview.get("user") if isinstance(preview, dict) else None
        if not isinstance(user, dict):
            continue
        user_id = str(user.get("id") or "")
        name = str(user.get("name") or "").strip()
        if not user_id.isdigit() or not name:
            continue
        illusts = preview.get("illusts") if isinstance(preview.get("illusts"), list) else []
        cover = ""
        if illusts and isinstance(illusts[0], dict):
            urls = illusts[0].get("url") or illusts[0].get("image_urls") or ""
            if isinstance(urls, dict):
                urls = urls.get("square_medium") or urls.get("medium") or ""
            cover = str(urls or "")
        hits.append(
            ArtHit(
                title=name,
                description="pixiv artist",
                external_url=f"https://www.pixiv.net/users/{user_id}",
                thumbnail_url=cover or None,
            )
        )
    return hits


async def search_pixiv_works(settings: Settings, query: str) -> list[ArtHit]:
    """Works by tag/title; each result downloads as one artwork."""
    if not pixiv.credentials_available(settings):
        raise pixiv.PixivError(
            "Pixiv needs a login for search. Re-run `gallery-dl oauth:pixiv`."
        )
    client = pixiv._client(settings)
    payload = await pixiv._app_get(
        settings,
        client,
        "/v1/search/illust",
        {"word": query, "limit": _RESULT_LIMIT},
    )
    hits: list[ArtHit] = []
    for item in payload.get("illusts") or []:
        work = pixiv._work_from_json(item)
        if work is None:
            continue
        hits.append(
            ArtHit(
                title=work.title or work.work_id,
                description=f"{work.artist} · {work.pages} p.",
                external_url=work.url,
                thumbnail_url=None,  # i.pximg.net answers 403 to Telegram
            )
        )
    return hits


async def _pawchive_creators(settings: Settings) -> list[dict]:
    """The creator index, fetched once per process and filtered by name."""
    key = settings.pawchive_root
    if key in _creators:
        return _creators[key]
    async with _creators_lock:
        if key in _creators:
            return _creators[key]
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as client, client.get(
            f"{key}/api/v1/creators",
            headers={"User-Agent": "tg-media-downloader"},
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, list):
            raise RuntimeError("pawchive creators: malformed payload")
        # Only creators with imported posts can produce a download.
        _creators[key] = [
            creator
            for creator in payload
            if isinstance(creator, dict) and creator.get("ever_imported")
        ]
        logger.info("Pawchive creators indexed | total=%s", len(_creators[key]))
    return _creators[key]


async def search_pawchive(settings: Settings, query: str) -> list[ArtHit]:
    """Creators by name from the cached index; a hit opens its post page."""
    creators = await _pawchive_creators(settings)
    lowered = query.lower()
    matches = [
        creator
        for creator in creators
        if lowered in str(creator.get("name") or "").lower()
    ]
    return [
        ArtHit(
            title=str(creator.get("name") or creator.get("id")),
            description=f"pawchive · {creator.get('service')}",
            external_url=(
                f"{settings.pawchive_root}/{creator.get('service')}/user/{creator.get('id')}"
            ),
        )
        for creator in matches[:_RESULT_LIMIT]
    ]


async def search_pawchive_posts(settings: Settings, query: str) -> list[ArtHit]:
    """Posts by title/description through the public post search."""
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as client, client.get(
        f"{settings.pawchive_root}/api/v1/posts",
        params={"q": query, "o": 0},
        headers={"User-Agent": "tg-media-downloader"},
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    hits: list[ArtHit] = []
    for post in payload if isinstance(payload, list) else []:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "")
        user = str(post.get("user") or "")
        service = str(post.get("service") or "")
        if not (post_id and user and service):
            continue
        title = str(post.get("title") or "").strip() or f"post {post_id}"
        hits.append(
            ArtHit(
                title=title[:64],
                description=f"{service} post",
                external_url=(
                    f"{settings.pawchive_root}/{service}/user/{user}/post/{post_id}"
                ),
            )
        )
    return hits[:_RESULT_LIMIT]


async def pawchive_creator_posts(
    settings: Settings, service: str, user_id: str, offset: int, limit: int = 10
) -> tuple[list[tuple[str, str, str]], int | None]:
    """One window of a creator's posts plus the next offset. Each entry is
    ``(title, post_id, thumbnail_url)``; the thumbnail comes from the post's
    first file path, which Pawchive serves on ``img.<host>/thumbnail``.
    ``None`` when no further page follows. Posts come back newest-first."""
    timeout = aiohttp.ClientTimeout(total=30)
    # The API pages by 50 and rejects a non-multiple offset; align down.
    batch = 50
    api_offset = offset - offset % batch
    params: dict[str, int] = {"o": api_offset}
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as client, client.get(
        f"{settings.pawchive_root}/api/v1/{service}/user/{user_id}",
        params=params,
        headers={"User-Agent": "tg-media-downloader"},
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    # The API returns a full page regardless of any limit; slice locally and
    # skip the items before the requested offset.
    wanted = offset - api_offset
    img_root = settings.pawchive_root.replace("://", "://img.") + "/thumbnail/data"
    posts: list[tuple[str, str, str]] = []
    for post in payload if isinstance(payload, list) else []:
        if not isinstance(post, dict):
            continue
        post_id = str(post.get("id") or "")
        if not post_id:
            continue
        if wanted > 0:
            wanted -= 1
            continue
        if len(posts) >= limit:
            break
        title = str(post.get("title") or "").strip() or f"post {post_id}"
        thumb = ""
        first = post.get("file") if isinstance(post.get("file"), dict) else None
        path = str(first.get("path") or "") if first else ""
        if path:
            thumb = img_root + path
        posts.append((title[:64], post_id, thumb))
    next_offset = offset + len(posts) if len(posts) >= limit else None
    return posts, next_offset
