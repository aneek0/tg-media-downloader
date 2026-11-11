"""Pawchive creator profile: URL detection, page slicing and persistence."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers.intake import _is_art_profile_url, _pawchive_user_parts
from services import art_search, pawchive_flow
from tests.conftest import make_session, make_settings
from utils.models import ParsedInput, PawchiveProfilePayload, RequestType


@pytest.fixture(autouse=True)
def _clean_pages():
    pawchive_flow._PAGES.clear()
    yield
    pawchive_flow._PAGES.clear()


# --- URL detection --------------------------------------------------------


def test_pawchive_user_parts_parses_creator_page() -> None:
    assert _pawchive_user_parts(
        "https://pawchive.pw/patreon/user/30500811"
    ) == ("patreon", "30500811")
    assert _pawchive_user_parts(
        "https://www.pawchive.pw/fanbox/user/123"
    ) == ("fanbox", "123")


def test_pawchive_user_parts_rejects_other_urls() -> None:
    assert _pawchive_user_parts("https://pawchive.pw/posts") is None
    assert _pawchive_user_parts("https://pixiv.net/users/1") is None
    assert _pawchive_user_parts("not-a-url") is None


def test_art_profile_url_accepts_pixiv_and_pawchive_profiles() -> None:
    assert _is_art_profile_url("https://www.pixiv.net/users/7918112")
    assert _is_art_profile_url("https://pawchive.pw/patreon/user/1")
    # Catalogue artist links inserted from our own inline search must also
    # flow into the DM pipeline, not be swallowed by the feedback gate.
    assert _is_art_profile_url("https://www.deezer.com/artist/1")
    assert not _is_art_profile_url("https://www.pixiv.net/artworks/1")
    assert not _is_art_profile_url("")

# --- pagination -----------------------------------------------------------


def _fake_posts(payload):
    response = SimpleNamespace(
        status=200, raise_for_status=lambda: None, json=AsyncMock(return_value=payload)
    )

    @asynccontextmanager
    async def _get(*_args, **_kwargs):
        yield response

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return SimpleNamespace(get=_get)

        async def __aexit__(self, *_a):
            return False

    return _Session


def _post(post_id: str, title: str | None = None) -> dict:
    return {"id": post_id, "title": title or f"post {post_id}"}


@pytest.mark.asyncio
async def test_creator_posts_slices_an_api_page_locally(tmp_path, monkeypatch):
    """The API returns a full 50-post page regardless of any limit; the
    provider must slice to the requested page size and paginate by offset."""
    settings = make_settings(tmp_path)
    payload = [_post(str(i), f"post {i}") for i in range(60)]
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_posts(payload)
    )
    posts, next_offset = await art_search.pawchive_creator_posts(
        settings, "patreon", "1", 0, limit=10
    )
    assert len(posts) == 10
    assert posts[0] == ("post 0", "0", "")
    assert next_offset == 10


@pytest.mark.asyncio
async def test_creator_posts_aligns_offset_to_api_batch(tmp_path, monkeypatch):
    """Non-multiple offsets still work: the fetch aligns down to 50 and
    skips the already-seen posts, so page 3 of 10 is correct."""
    settings = make_settings(tmp_path)
    payload = [_post(str(i), f"post {i}") for i in range(50)]
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_posts(payload)
    )
    posts, next_offset = await art_search.pawchive_creator_posts(
        settings, "patreon", "1", 20, limit=10
    )
    assert [pid for _, pid, _ in posts] == [str(i) for i in range(20, 30)]
    assert next_offset == 30


@pytest.mark.asyncio
async def test_creator_posts_reports_end_of_list(tmp_path, monkeypatch):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_posts([_post("1")])
    )
    posts, next_offset = await art_search.pawchive_creator_posts(
        settings, "patreon", "1", 0, limit=10
    )
    assert len(posts) == 1
    assert next_offset is None



@pytest.mark.asyncio
async def test_creator_posts_builds_thumbnail_from_first_file(tmp_path, monkeypatch):
    """Each post carries a thumbnail URL from its first file's path; Pawchive
    serves previews on img.<host>/thumbnail, which Telegram can fetch itself."""
    settings = make_settings(tmp_path)
    payload = [
        {
            "id": "1",
            "title": "post 1",
            "file": {
                "path": "/db/4f/db4fc14a92418260245afa0b5f2eb3aee35539946498b30b19fcbda1a693a715.jpg",
            },
        },
        {"id": "2", "title": "post 2", "file": {}},  # no path -> no thumb
    ]
    monkeypatch.setattr(
        art_search.aiohttp, "ClientSession", _fake_posts(payload)
    )
    posts, _ = await art_search.pawchive_creator_posts(
        settings, "patreon", "1", 0, limit=10
    )
    assert posts[0][2] == (
        "https://img.pawchive.pw/thumbnail/data"
        "/db/4f/db4fc14a92418260245afa0b5f2eb3aee35539946498b30b19fcbda1a693a715.jpg"
    )
    assert posts[1][2] == ""


# --- persistence ----------------------------------------------------------

def test_pawchive_payload_round_trips_through_the_store(tmp_path: Path) -> None:
    session = make_session(make_settings(tmp_path))
    stored = session.request_store.open(
        RequestType.PAWCHIVE_PROFILE,
        ParsedInput(source_url="https://pawchive.pw/patreon/user/1"),
        payload=PawchiveProfilePayload(
            service="patreon",
            user_id="1",
            user_name="artist",
            offsets=[0, 10],
            more=[True, False],
            post_ids=[["a", "b"], ["c"]],
            post_thumbs=[["t1", "t2"], ["t3"]],
            preview_ids=[10, 11],
        ),
    )
    assert stored.request_type is RequestType.PAWCHIVE_PROFILE
    loaded = session.request_store.load(stored.token)
    assert loaded is not None
    payload = loaded.payload
    assert isinstance(payload, PawchiveProfilePayload)
    assert payload.service == "patreon"
    assert payload.user_id == "1"
    assert payload.post_ids == [["a", "b"], ["c"]]
    assert payload.post_thumbs == [["t1", "t2"], ["t3"]]
    assert payload.preview_ids == [10, 11]