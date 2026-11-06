from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram.exceptions import TelegramBadRequest

from services.catalogue_search import ArtistCatalogue, CatalogueSearch, SearchItem
from services.follows import FollowPoller
from tests.conftest import make_session, make_settings


def _artist() -> SearchItem:
    return SearchItem(
        provider="deezer",
        kind="artist",
        item_id="deezer:1",
        title="Artist",
        cover_url="https://example.com/cover.jpg",
        external_url="https://www.deezer.com/artist/1",
    )


def _album(index: int) -> SearchItem:
    return SearchItem(
        provider="deezer",
        kind="album",
        item_id=f"deezer:{index}",
        title=f"Release {index}",
        release_date="2026-11-04",
        external_url=f"https://www.deezer.com/album/{index}",
    )


@pytest.mark.asyncio
async def test_follow_baseline_does_not_notify_and_caps_new_release_list(
    monkeypatch, tmp_path
):
    """Existing releases stay silent; one later notification shows at most five."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    session.follows.toggle(
        99,
        "deezer",
        "deezer:1",
        "Artist",
        cover_url="cover",
        known_album_ids=["deezer:1"],
    )
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(
        session.catalogue_search,
        "artist",
        AsyncMock(
            return_value=ArtistCatalogue(artist=_artist(), albums=[_album(1)])
        ),
    )
    poller = FollowPoller(session, bot)
    await poller.poll_once()
    bot.send_photo.assert_not_awaited()

    albums = [_album(index) for index in range(1, 8)]
    monkeypatch.setattr(
        session.catalogue_search,
        "artist",
        AsyncMock(return_value=ArtistCatalogue(artist=_artist(), albums=albums)),
    )
    await poller.poll_once()
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert "Release 6" in caption
    assert "Release 7" not in caption
    assert session.follows.list_for_user(99)[0].known_album_ids == [
        album.item_id for album in albums
    ]


@pytest.mark.asyncio
async def test_failed_recipient_keeps_own_release_baseline(monkeypatch, tmp_path):
    """A blocked DM must not consume releases for that user or block another."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    for user_id in (99, 100):
        session.follows.toggle(
            user_id,
            "deezer",
            "deezer:1",
            "Artist",
            cover_url="cover",
            known_album_ids=["deezer:1"],
        )
    monkeypatch.setattr(
        session.catalogue_search,
        "artist",
        AsyncMock(
            return_value=ArtistCatalogue(
                artist=_artist(), albums=[_album(1), _album(2)]
            )
        ),
    )
    bot = SimpleNamespace(
        send_photo=AsyncMock(
            side_effect=[None, TelegramBadRequest(method=Mock(), message="blocked")]
        ),
        send_message=AsyncMock(
            side_effect=TelegramBadRequest(method=Mock(), message="blocked")
        ),
    )
    await FollowPoller(session, bot).poll_once()
    assert session.follows.list_for_user(99)[0].known_album_ids == [
        "deezer:1",
        "deezer:2",
    ]
    assert session.follows.list_for_user(100)[0].known_album_ids == ["deezer:1"]


class _CatalogueExtension:
    """Extension double answering ``getArtist`` for every requested artist."""

    def __init__(self) -> None:
        self.closed = False

    def _call(self, _method: str, *args):
        artist_id = args[0]
        return {
            "id": artist_id,
            "name": "Artist",
            "albums": [
                {
                    "item_type": "album",
                    "id": f"{artist_id}:fresh",
                    "name": "Fresh",
                    "release_date": "2026-11-05",
                }
            ],
            "top_tracks": [],
        }

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_deezer_top_timeout_skips_one_artist_and_keeps_polling(
    monkeypatch, tmp_path
):
    """A timed-out artist must not stop other artists or later polling rounds."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    session.follows.toggle(99, "deezer", "deezer:1", "Artist One")
    session.follows.toggle(
        99,
        "deezer",
        "deezer:2",
        "Artist Two",
        cover_url="cover",
        known_album_ids=["old:1", "old:2", "old:3"],
    )
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    monkeypatch.setattr(
        "services.catalogue_search._open_provider",
        lambda provider: _CatalogueExtension(),
    )
    async def fail_first_artist(_self, artist_id):
        if artist_id == "deezer:1":
            raise asyncio.TimeoutError("simulated timeout")
        return []

    monkeypatch.setattr(
        CatalogueSearch,
        "_deezer_top",
        fail_first_artist,
    )
    poller = FollowPoller(session, bot)

    await poller.poll_once()
    await poller.poll_once()

    caption = bot.send_photo.await_args.kwargs["caption"]
    assert "Artist Two" in caption and "Fresh" in caption
    assert bot.send_photo.await_count == 1
    failed = {
        follow.artist_id: follow for follow in session.follows.list_for_user(99)
    }["deezer:1"]
    assert failed.known_album_ids == []
