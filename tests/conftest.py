from __future__ import annotations

import asyncio

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import Settings
from services.cooldown import CooldownManager
from services.catalogue_search import CatalogueSearch, SearchItem
from services.follow_store import FollowStore
from services.media_cache import MediaCache
from services.mornye import MornyeService
from services.request_store import RequestStore
from services.session import Session
from services.user_settings import UserSettingsStore
from utils.models import CachedMedia


def make_artist_search_item() -> SearchItem:
    return SearchItem(
        provider="deezer",
        kind="artist",
        item_id="deezer:1",
        title="Artist",
        cover_url="https://example.com/artist.jpg",
        external_url="https://www.deezer.com/artist/1",
        listeners=123,
    )


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        bot_token="token",
        owner_id=99,
        auth_users={99},
        download_location=tmp_path / "downloads",
        chunk_size=1024,
        http_proxy="",
        process_max_timeout=120,
        auto_best_quality=False,
        max_video_height=1080,
        gallery_probe_timeout=15,
        verify_ssl=True,
        twitter_cookies="",
        telegram_api_url="",
        telegram_proxy="",
    )


def make_media_cache(tmp_path: Path) -> MediaCache:
    return MediaCache(tmp_path / "media_cache.json")


async def record_cached_video(cache: MediaCache, url: str) -> None:
    """Seed the media cache with one uploaded video, as a CDN hit would."""
    await cache.record(
        url,
        [
            CachedMedia(
                file_id="BAAC9", send_type="video", file_name="f.mp4", caption="c"
            )
        ],
    )


def make_message():
    user = SimpleNamespace(id=99, first_name="Kalan")
    chat = SimpleNamespace(id=500, type="private")
    return SimpleNamespace(
        from_user=user,
        chat=chat,
        text=None,
        entities=None,
        answer=AsyncMock(),
        reply=AsyncMock(),
        answer_photo=AsyncMock(),
        reply_media_group=AsyncMock(),
        reply_photo=AsyncMock(),
    )


def make_session(
    settings: Settings,
    request_store: RequestStore | None = None,
    media_cache: MediaCache | None = None,
    mornye: MornyeService | None = None,
) -> Session:
    """Session over test doubles; stores default to real ones under the
    Settings paths so tmp_path isolation holds."""
    return Session(
        settings=settings,
        cooldown=CooldownManager(
            timeout_seconds=settings.request_cooldown_seconds
        ),
        request_store=request_store
        or RequestStore(settings.requests_dir, settings.work_dir),
        media_cache=media_cache or MediaCache(settings.media_cache_file),
        mornye=mornye or MornyeService(settings),
        catalogue_search=CatalogueSearch(provider=settings.search_provider),
        user_settings=UserSettingsStore(settings.user_settings_file),
        follows=FollowStore(settings.follows_file),
        gallery_download_limiter=asyncio.Semaphore(6),
    )
