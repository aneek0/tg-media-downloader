from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import InlineQueryResultArticle, Update

from app import create_dispatcher
from routers.catalogue import (
    _LAST_SEARCH,
    _SEARCH_CACHE,
    catalogue_callback,
    catalogue_chosen,
    catalogue_query,
)
from services.catalogue_search import (
    CatalogueNetworkError,
    SearchItem,
    SearchResults,
)
from tests.conftest import make_artist_search_item as _artist
from tests.conftest import make_session, make_settings
from utils.callbacks import CatalogueCallback, join_token
from utils.models import (
    CataloguePayload,
    InlineMediaPayload,
    ParsedInput,
    RequestType,
)


def _query(text: str, chat_type: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        query=text,
        chat_type=chat_type,
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
    stored = session.request_store.open(
        RequestType.INLINE_MEDIA,
        ParsedInput(source_url="https://www.deezer.com/track/1"),
        payload=InlineMediaPayload(),
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
async def test_top_and_menu_callbacks_switch_artist_views(monkeypatch, tmp_path):
    """TOP opens downloads separately and BACK restores the compact artist menu."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url="https://www.deezer.com/artist/1"),
        payload=CataloguePayload(
            provider="deezer",
            artist_id="deezer:1",
            name="Artist",
            top_tracks=[_found().items[0].to_dict()],
        ),
    )
    show_card = AsyncMock()
    monkeypatch.setattr("routers.catalogue.present_card", show_card)
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        inline_message_id="inline-id",
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )

    await catalogue_callback(
        query, CatalogueCallback(action="top", token=stored.token), session
    )

    top_card = show_card.await_args.args[0]
    assert len(top_card.keyboard.inline_keyboard) == 2
    assert CatalogueCallback.unpack(
        top_card.keyboard.inline_keyboard[0][0].callback_data
    ).action == "dl"
    assert CatalogueCallback.unpack(
        top_card.keyboard.inline_keyboard[-1][0].callback_data
    ).action == "menu"

    await catalogue_callback(
        query, CatalogueCallback(action="menu", token=stored.token), session
    )

    menu_card = show_card.await_args.args[0]
    assert [
        [button.text for button in row] for row in menu_card.keyboard.inline_keyboard
    ] == [["TOP 30 🔝", "ALBUMS 💽"], ["FOLLOW ➕"]]


@pytest.mark.asyncio
async def test_follow_toggle_rerenders_artist_card(monkeypatch, tmp_path):
    """Follow must update the visible toggle instead of returning before the rerender."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url="https://www.deezer.com/artist/1"),
        payload=CataloguePayload(provider="deezer", artist_id="deezer:1", name="Artist"),
    )
    show_card = AsyncMock()
    monkeypatch.setattr("routers.catalogue.present_card", show_card)
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
    card = show_card.await_args.args[0]
    assert any(
        button.text == "FOLLOWING ✓"
        for row in card.keyboard.inline_keyboard
        for button in row
    )

@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "sender"])
async def test_private_artist_feedback_inserts_url_without_inline_edit(
    monkeypatch, tmp_path, chat_type
):
    """DM artist feedback hands its URL to the normal bot-authored intake flow."""
    _SEARCH_CACHE.clear()
    _LAST_SEARCH.clear()
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    monkeypatch.setattr(
        session.catalogue_search,
        "search",
        AsyncMock(return_value=SearchResults(items=[_artist()])),
    )
    query = _query(".art Artist", chat_type)

    await catalogue_query(query, session)

    result = query.answer.await_args.args[0][0]
    assert isinstance(result, InlineQueryResultArticle)
    assert result.id.startswith("artist-link:")
    assert result.title == "🎤 Artist"
    assert result.thumbnail_url == _artist().cover_url
    assert result.input_message_content.message_text == _artist().external_url
    assert result.reply_markup is None
    chosen = SimpleNamespace(result_id=result.id)
    assert await catalogue_chosen(chosen, session) is None


@pytest.mark.asyncio
async def test_public_artist_feedback_keeps_inline_artist_card(monkeypatch, tmp_path):
    """Outside DM, artist selection retains its browse token and inline keyboard."""
    _SEARCH_CACHE.clear()
    _LAST_SEARCH.clear()
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    monkeypatch.setattr(
        session.catalogue_search,
        "search",
        AsyncMock(return_value=SearchResults(items=[_artist()])),
    )
    query = _query(".art Artist", "group")

    await catalogue_query(query, session)

    result = query.answer.await_args.args[0][0]
    assert isinstance(result, InlineQueryResultArticle)
    assert result.id.startswith("browse:")
    assert result.title == "🎤 Artist"
    assert result.thumbnail_url == _artist().cover_url
    assert result.reply_markup is not None
    stored = session.request_store.load(result.id.split(":", 1)[1])
    payload = stored.payload
    assert isinstance(payload, CataloguePayload)
    assert payload.artist_id == "deezer:1"


@pytest.mark.asyncio
async def test_bot_artist_card_callbacks_edit_normal_message(tmp_path):
    """TOP edits the bot's DM photo instead of requiring an inline_message_id."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    stored = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url=_artist().external_url),
        payload=CataloguePayload(
            provider="deezer",
            artist_id="deezer:1",
            name="Artist",
            cover_url=_artist().cover_url,
            top_tracks=[_found().items[0].to_dict()],
        ),
    )
    message = SimpleNamespace(photo=[SimpleNamespace()], edit_media=AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        message=message,
        inline_message_id=None,
        answer=AsyncMock(),
        bot=SimpleNamespace(edit_message_media=AsyncMock()),
    )

    await catalogue_callback(
        query, CatalogueCallback(action="top", token=stored.token), session
    )

    message.edit_media.assert_awaited_once()
    query.bot.edit_message_media.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_artist_card_download_uses_normal_executor(monkeypatch, tmp_path):
    """Artist Card tracks in DM use chat delivery rather than inline media edits."""
    settings = make_settings(tmp_path)
    settings.ensure_directories()
    session = make_session(settings)
    card = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url=_artist().external_url),
        payload=CataloguePayload(
            provider="deezer",
            artist_id="deezer:1",
            name="Artist",
            top_tracks=[_found().items[0].to_dict()],
        ),
    )
    status_message = SimpleNamespace(edit_text=AsyncMock())
    message = SimpleNamespace(reply=AsyncMock(return_value=status_message))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        message=message,
        inline_message_id=None,
        answer=AsyncMock(),
        bot=SimpleNamespace(),
    )
    execute = AsyncMock()
    inline_download = AsyncMock()
    monkeypatch.setattr("routers.catalogue.execute_request", execute)
    monkeypatch.setattr("routers.catalogue.run_inline_download", inline_download)

    await catalogue_callback(
        query,
        CatalogueCallback(action="dl", token=join_token(card.token, 0)),
        session,
    )

    assert execute.await_args.kwargs["source_message"] is message
    inline_download.assert_not_awaited()
