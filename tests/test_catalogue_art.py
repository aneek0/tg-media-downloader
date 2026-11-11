"""Catalogue router art-source search: pixiv/pawchive inline results."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED

from routers.catalogue import _LAST_SEARCH, catalogue_query
from services.art_search import ArtHit
from tests.conftest import make_session, make_settings
from utils.models import RequestType


def _query(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        query=text,
        chat_type="private",
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )


@pytest.fixture(autouse=True)
def _clean_rate_limit():
    _LAST_SEARCH.clear()
    yield
    _LAST_SEARCH.clear()


@pytest.mark.asyncio
async def test_pxv_prefix_returns_pixiv_hits(monkeypatch, tmp_path):
    """.pxv overrides the provider for one query: artist articles first,
    then one download article per work."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    artists = [
        ArtHit(
            title="Mika Pikazo",
            description="pixiv artist",
            external_url="https://www.pixiv.net/users/1039353",
        )
    ]
    works = [
        ArtHit(
            title="winter",
            description="HongMoruChan · 2 p.",
            external_url="https://www.pixiv.net/artworks/151920832",
        )
    ]
    monkeypatch.setattr(
        "routers.catalogue.art_search.search_pixiv", AsyncMock(return_value=artists)
    )
    monkeypatch.setattr(
        "routers.catalogue.art_search.search_pixiv_works",
        AsyncMock(return_value=works),
    )
    query = _query(".pxv mika")

    await catalogue_query(query, session)

    results = query.answer.await_args.args[0]
    assert len(results) == 2
    artist, work = results
    assert artist.id == "artist-link:https://www.pixiv.net/users/1039353"
    assert artist.input_message_content.message_text == (
        "https://www.pixiv.net/users/1039353"
    )
    assert work.title == "🖼 winter"
    stored = session.request_store.load(work.id)
    assert stored is not None
    assert stored.request_type is RequestType.INLINE_MEDIA
    assert stored.parsed_input.source_url == "https://www.pixiv.net/artworks/151920832"


@pytest.mark.asyncio
async def test_paw_prefix_returns_pawchive_hits(monkeypatch, tmp_path):
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    monkeypatch.setattr(
        "routers.catalogue.art_search.search_pawchive",
        AsyncMock(
            return_value=[
                ArtHit(
                    title="BOKABA",
                    description="pawchive · patreon",
                    external_url="https://pawchive.pw/patreon/user/1",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        "routers.catalogue.art_search.search_pawchive_posts",
        AsyncMock(return_value=[]),
    )
    query = _query(".paw bokab")

    await catalogue_query(query, session)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].id == "artist-link:https://pawchive.pw/patreon/user/1"


@pytest.mark.asyncio
async def test_art_prefix_without_word_is_unhandled(monkeypatch, tmp_path):
    """.pxv alone must not eat the query; the music ladder never runs."""
    settings = make_settings(tmp_path)
    session = make_session(settings)
    search = AsyncMock()
    monkeypatch.setattr(session.catalogue_search, "search", search)
    query = _query(".pxv")

    result = await catalogue_query(query, session)

    assert result is UNHANDLED
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_art_search_error_answers_one_article(monkeypatch, tmp_path):
    """A failing art search shows the error inline instead of raising."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    monkeypatch.setattr(
        "routers.catalogue.art_search.search_pawchive",
        AsyncMock(side_effect=RuntimeError("pawchive unreachable")),
    )
    query = _query(".paw anything")

    await catalogue_query(query, session)

    results = query.answer.await_args.args[0]
    assert len(results) == 1
    assert results[0].id == "art-search-error"
    assert "pawchive unreachable" in results[0].input_message_content.message_text
