"""Art text search: Pixiv and Pawchive providers behind the inline search."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import art_search
from tests.conftest import make_settings


@pytest.fixture(autouse=True)
def _clean_creators_cache():
    art_search._creators.clear()
    yield
    art_search._creators.clear()


def _settings(tmp_path: Path):
    return make_settings(tmp_path)


def _fake_client(payload):
    """aiohttp session stub answering the one GET with ``payload``."""
    response = SimpleNamespace(
        status=200,
        raise_for_status=lambda: None,
        json=AsyncMock(return_value=payload),
    )

    @asynccontextmanager
    async def _get(*_args, **_kwargs):
        yield response

    session = SimpleNamespace(get=_get)

    @asynccontextmanager
    async def _session(*_args, **_kwargs):
        yield session

    return _session



async def test_search_pixiv_normalizes_user_previews(tmp_path, monkeypatch):
    payload = {
        "user_previews": [
            {
                "user": {"id": 1039353, "name": "Mika Pikazo"},
                "illusts": [
                    {"url": "https://i.pximg.net/c/square/img/a.jpg"}
                ],
            },
            {"user": {"id": 0, "name": "broken"}},  # no id -> dropped
            "junk",
        ]
    }
    monkeypatch.setattr(art_search.pixiv, "credentials_available", lambda s: True)
    monkeypatch.setattr(
        art_search.pixiv,
        "_app_get",
        AsyncMock(return_value=payload),
    )
    hits = await art_search.search_pixiv(_settings(tmp_path), "mika")
    assert len(hits) == 1
    assert hits[0].title == "Mika Pikazo"
    assert hits[0].external_url == "https://www.pixiv.net/users/1039353"
    assert hits[0].thumbnail_url == "https://i.pximg.net/c/square/img/a.jpg"


async def test_search_pixiv_works_never_expose_pximg_thumbnails(tmp_path, monkeypatch):
    """i.pximg.net answers 403 to Telegram's fetcher, so a work hit must not
    carry a thumbnail even when the API returns one."""
    payload = {
        "illusts": [
            {
                "id": 151920832,
                "title": "winter",
                "type": "illust",
                "page_count": 2,
                "user": {"id": 130740143, "name": "HongMoruChan"},
                "image_urls": {"square_medium": "https://i.pximg.net/x.jpg"},
            }
        ]
    }
    monkeypatch.setattr(art_search.pixiv, "credentials_available", lambda s: True)
    monkeypatch.setattr(
        art_search.pixiv, "_app_get", AsyncMock(return_value=payload)
    )
    hits = await art_search.search_pixiv_works(_settings(tmp_path), "winter")
    assert len(hits) == 1
    assert hits[0].external_url == "https://www.pixiv.net/artworks/151920832"
    assert hits[0].description == "HongMoruChan · 2 p."
    assert hits[0].thumbnail_url is None


# --- Pawchive -------------------------------------------------------------


async def test_search_pawchive_filters_creators_by_name(tmp_path, monkeypatch):
    creators = [
        {"id": "1", "name": "BOKABA", "service": "patreon", "ever_imported": True},
        {"id": "2", "name": "bokabaka", "service": "fanbox", "ever_imported": True},
        {"id": "3", "name": "unrelated", "service": "patreon", "ever_imported": True},
        {"id": "4", "name": "BOKABA-ghost", "service": "patreon", "ever_imported": False},
    ]
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_client(creators)
    )
    hits = await art_search.search_pawchive(_settings(tmp_path), "bokab")
    assert [hit.title for hit in hits] == ["BOKABA", "bokabaka"]
    assert hits[0].external_url.endswith("/patreon/user/1")
    # The index is fetched once; a second search reuses the cached copy.
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_client([])
    )
    again = await art_search.search_pawchive(_settings(tmp_path), "bokab")
    assert [hit.title for hit in again] == ["BOKABA", "bokabaka"]


async def test_search_pawchive_posts_builds_post_urls(tmp_path, monkeypatch):
    posts = [
        {
            "id": "172966005",
            "user": "143623028",
            "service": "patreon",
            "title": "Chapter 171",
        },
        {"id": "", "user": "1", "service": "patreon"},  # dropped
        "junk",
    ]
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_client(posts)
    )
    hits = await art_search.search_pawchive_posts(_settings(tmp_path), "chapter")
    assert len(hits) == 1
    assert hits[0].external_url.endswith("/patreon/user/143623028/post/172966005")
    assert hits[0].title == "Chapter 171"
