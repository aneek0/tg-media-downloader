from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from app import create_dispatcher
from routers.catalogue import _LAST_SEARCH, _SEARCH_CACHE, catalogue_callback, catalogue_query
from services.catalogue_search import (
    CatalogueNetworkError,
    SearchItem,
    SearchResults,
)
from services.requests import catalogue_browse_request, inline_media_request
from tests.conftest import make_session, make_settings
from utils.callbacks import CatalogueCallback
from utils.models import CataloguePayload, ParsedInput


def _query(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        query=text,
        from_user=SimpleNamespace(id=99),
        answer=AsyncMock(),
    )


def _found() -> SearchResults:
    return SearchResults(
        items=[
            SearchItem(
                provider="deezer",
                kind="track",
                item_id="deezer:1",
                title="Song",
                artists="Artist",
                cover_url="https://example.com/cover.jpg",
                external_url="https://www.deezer.com/track/1",
            )
        ]
    )


@pytest.mark.asyncio
async def test_filtered_search_uses_eight_and_memory_cache(monkeypatch, tmp_path):
    """Filtered queries use eight results and identical repeats avoid the API."""
    _SEARCH_CACHE.clear()
    _LAST_SEARCH.clear()
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    search = AsyncMock(return_value=_found())
    monkeypatch.setattr(session.catalogue_search, "search", search)
    first = _query(".trk Song")
    await catalogue_query(first, session)
    assert search.await_args.kwargs["per_kind"] == 8
    assert first.answer.await_args.kwargs["cache_time"] == 30

    second = _query(".trk Song")
    await catalogue_query(second, session)
    assert search.await_count == 1
    assert len(second.answer.await_args.args[0]) == 1


@pytest.mark.asyncio
async def test_global_search_uses_four_and_rate_limits_new_query(monkeypatch, tmp_path):
    """A different query inside 0.5 seconds never reaches the provider."""
    _SEARCH_CACHE.clear()
    _LAST_SEARCH.clear()
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    times = iter((100.0, 100.49, 100.5))
    monkeypatch.setattr("routers.catalogue.monotonic", lambda: next(times))
    search = AsyncMock(return_value=_found())
    monkeypatch.setattr(session.catalogue_search, "search", search)
    await catalogue_query(_query("Song"), session)
    assert search.await_args.kwargs["per_kind"] == 4

    limited = _query("Another")
    await catalogue_query(limited, session)
    assert search.await_count == 1
    result = limited.answer.await_args.args[0][0]
    assert result.id == "catalogue-rate-limit"
    await catalogue_query(_query("Boundary"), session)
    assert search.await_count == 2


@pytest.mark.asyncio
async def test_track_chosen_result_reaches_inline_download(monkeypatch, tmp_path):
    """Catalogue browsing must not swallow track choices for the inline downloader."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = inline_media_request(
        session.request_store,
        ParsedInput(source_url="https://www.deezer.com/track/1"),
    )
    runner = AsyncMock()
    monkeypatch.setattr("routers.inline.run_inline_download", runner)
    bot = Bot("123:abc")
    try:
        dispatcher = create_dispatcher(session)
        update = Update.model_validate(
            {
                "update_id": 1,
                "chosen_inline_result": {
                    "result_id": stored.token,
                    "from": {"id": 99, "is_bot": False, "first_name": "T"},
                    "inline_message_id": "inline-id",
                    "query": "test",
                },
            }
        )
        await dispatcher.feed_update(bot, update)
        await asyncio.sleep(0)
        assert runner.await_count == 1
    finally:
        await bot.session.close()


@pytest.mark.asyncio
async def test_url_query_is_unhandled_for_original_inline_router(tmp_path):
    """URL inline queries must continue to the pre-existing URL pipeline."""
    settings = make_settings(tmp_path)
    session = make_session(settings)
    assert await catalogue_query(_query("https://example.com/video"), session) is UNHANDLED


@pytest.mark.asyncio
async def test_network_failure_returns_retryable_error_not_empty_results(
    monkeypatch, tmp_path
):
    """A provider outage must not look like a successful empty search."""
    _SEARCH_CACHE.clear()
    _LAST_SEARCH.clear()
    session = make_session(make_settings(tmp_path))
    monkeypatch.setattr(
        session.catalogue_search,
        "search",
        AsyncMock(side_effect=CatalogueNetworkError("provider connection failed")),
    )
    query = _query(".trk Song")

    await catalogue_query(query, session)

    result = query.answer.await_args.args[0][0]
    assert result.id == "catalogue-network-error"
    assert "Network error" in result.title
    assert "try again" in result.input_message_content.message_text
    assert "provider connection failed" not in result.input_message_content.message_text


@pytest.mark.asyncio
async def test_follow_toggle_rerenders_artist_card(monkeypatch, tmp_path):
    """Follow must update the visible toggle instead of returning before the rerender."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = catalogue_browse_request(
        session.request_store,
        ParsedInput(source_url="https://www.deezer.com/artist/1"),
        CataloguePayload(provider="deezer", artist_id="deezer:1", name="Artist"),
    )
    show_card = AsyncMock()
    monkeypatch.setattr("routers.catalogue._show_card", show_card)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        inline_message_id="inline-id",
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )

    await catalogue_callback(
        query, CatalogueCallback(action="follow", token=stored.token), session
    )

    show_card.assert_awaited_once()
    card = show_card.await_args.args[3]
    assert any(
        button.text == "FOLLOWING ✓"
        for row in card.keyboard.inline_keyboard
        for button in row
    )
