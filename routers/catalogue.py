from __future__ import annotations

import logging
from time import monotonic
from typing import cast

from aiogram import Bot, Router
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultUnion,
    InputTextMessageContent,
)

from services.catalogue_flow import (
    CardSurface,
    album_card,
    album_download_request,
    artist_card,
    present_card,
    render_artist_card,
    run_album_zip,
    run_chat_album_zip,
    track_request,
)
from services.catalogue_search import (
    CatalogueNetworkError,
    SearchItem,
    SearchQuery,
    SearchResults,
)
from services.executor import execute_request
from services.inline_flow import run_inline_download
from services.pipeline import MUSIC_OPTIONS
from services.session import Session
from services.status import chat_status
from utils import text
from utils.callbacks import CatalogueCallback, InlineCancelCallback, split_token
from utils.models import (
    CataloguePayload,
    InlineMediaPayload,
    ParsedInput,
    RequestType,
    StoredRequest,
)

router = Router(name="catalogue")
logger = logging.getLogger(__name__)
_SEARCH_CACHE: dict[int, tuple[float, str, SearchResults]] = {}
_LAST_SEARCH: dict[int, float] = {}



def _cancel_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Cancel",
                    callback_data=InlineCancelCallback(token=token).pack(),
                )
            ]
        ]
    )


@router.inline_query()
async def catalogue_query(query: InlineQuery, session: Session) -> object | None:
    raw = query.query.strip()
    if not raw or "https://" in raw or "http://" in raw:
        return UNHANDLED
    if not query.from_user:
        return UNHANDLED
    preferences = session.preferences(query.from_user.id)
    if not preferences.text_search:
        return UNHANDLED
    user_id = query.from_user.id
    cache_key = f"{preferences.provider}:{raw}"
    now = monotonic()
    cached = _SEARCH_CACHE.get(user_id)
    try:
        parsed = SearchQuery.parse(raw)
        if cached and cached[1] == cache_key and now - cached[0] < 60:
            found = cached[2]
        else:
            if now - _LAST_SEARCH.get(user_id, 0) < 0.5:
                await query.answer(
                    [
                        InlineQueryResultArticle(
                            id="catalogue-rate-limit",
                            title=text.SEARCH_RATE_LIMIT,
                            input_message_content=InputTextMessageContent(
                                message_text=text.SEARCH_RATE_LIMIT
                            ),
                        )
                    ],
                    cache_time=1,
                )
                return
            _LAST_SEARCH[user_id] = now
            found = await session.catalogue_search.search(
                parsed,
                per_kind=8 if parsed.kind else 4,
                provider=preferences.provider,
            )
            _SEARCH_CACHE[user_id] = (now, cache_key, found)
    except (ValueError, RuntimeError) as exc:
        is_network_error = isinstance(exc, CatalogueNetworkError)
        error_message = (
            text.SEARCH_NETWORK_ERROR if is_network_error else text.esc(str(exc))
        )
        logger.info("Catalogue search failed | user=%s error=%s", query.from_user.id, exc)
        await query.answer(
            [
                InlineQueryResultArticle(
                    id=(
                        "catalogue-network-error"
                        if is_network_error
                        else "catalogue-error"
                    ),
                    title=(
                        text.SEARCH_NETWORK_ERROR
                        if is_network_error
                        else text.SEARCH_FAILED
                    ),
                    input_message_content=InputTextMessageContent(
                        message_text=error_message
                    ),
                )
            ],
            cache_time=1,
        )
        return

    private_handoff = getattr(query, "chat_type", None) in {"private", "sender"}
    results: list[InlineQueryResultUnion] = []
    for item in found.items:
        if item.kind == "artist":
            results.append(
                _artist_link_result(item)
                if private_handoff
                else _artist_result(session, item)
            )
        else:
            results.append(_media_result(session, item))
    await query.answer(results, cache_time=30, is_personal=True)


def _artist_link_result(item: SearchItem) -> InlineQueryResultArticle:
    """DM handoff: the inserted artist URL triggers a normal bot response."""
    description = f"👥 {item.listeners}" if item.listeners else text.ARTIST_CARD
    return InlineQueryResultArticle(
        id=f"artist-link:{item.provider}:{item.item_id}",
        title=f"🎤 {item.title}",
        description=description,
        thumbnail_url=item.cover_url or None,
        input_message_content=InputTextMessageContent(
            message_text=text.esc(item.external_url)
        ),
    )


def _artist_result(session: Session, item: SearchItem) -> InlineQueryResultArticle:
    stored = session.request_store.open(
        RequestType.CATALOGUE_BROWSE,
        ParsedInput(source_url=item.external_url),
        payload=CataloguePayload(
            provider=item.provider,
            artist_id=item.item_id,
            name=item.title,
            cover_url=item.cover_url,
            artist_url=item.external_url,
            listeners=item.listeners,

        ),
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=text.ARTIST_TOP,
                    callback_data=CatalogueCallback(
                        action="top", token=stored.token
                    ).pack(),
                )
            ]
        ]
    )
    description = f"👥 {item.listeners}" if item.listeners else text.ARTIST_CARD
    return InlineQueryResultArticle(
        id=f"browse:{stored.token}",
        title=f"🎤 {item.title}",
        description=description,
        thumbnail_url=item.cover_url or None,
        input_message_content=InputTextMessageContent(message_text=text.esc(item.title)),
        reply_markup=keyboard,
    )


def _media_result(session: Session, item: SearchItem) -> InlineQueryResultArticle:
    stored = session.request_store.open(
        RequestType.INLINE_MEDIA,
        ParsedInput(source_url=item.external_url),
        payload=InlineMediaPayload(plan="music"),
    )
    icons = {"track": "🎵", "album": "💽", "playlist": "🎶"}
    description = item.artists or item.album or item.external_url
    return InlineQueryResultArticle(
        id=stored.token,
        title=f"{icons.get(item.kind, '🎵')} {item.title}",
        description=description,
        thumbnail_url=item.cover_url or None,
        input_message_content=InputTextMessageContent(message_text=text.PROCESSING),
        reply_markup=_cancel_keyboard(stored.token),
    )


@router.chosen_inline_result()
async def catalogue_chosen(query: ChosenInlineResult, session: Session) -> object | None:
    if query.result_id.startswith("artist-link:"):
        return
    if not query.result_id.startswith("browse:"):
        return UNHANDLED
    token = query.result_id.split(":", 1)[1]
    stored = _load(session, token, RequestType.CATALOGUE_BROWSE)
    if stored is None:
        return
    payload = stored.payload
    if not isinstance(payload, CataloguePayload):
        return
    try:
        following = any(
            follow.provider == payload.provider and follow.artist_id == payload.artist_id
            for follow in session.follows.list_for_user(query.from_user.id)
        )
        card = await artist_card(
            session,
            provider=payload.provider,
            artist_id=payload.artist_id,
            token=token,
            following=following,
        )
        await present_card(
            card,
            bot=query.bot,
            surface=CardSurface.INLINE,
            inline_message_id=query.inline_message_id,
        )
    except (RuntimeError, ValueError, TelegramAPIError) as exc:
        logger.info("Artist Card failed | user=%s error=%s", query.from_user.id, exc)
        await _show_failure(query.bot, query.inline_message_id, stored, str(exc))


@router.callback_query(CatalogueCallback.filter())
async def catalogue_callback(  # noqa: C901
    query: CallbackQuery, callback_data: CatalogueCallback, session: Session
) -> None:
    if not query.from_user:
        return
    action = callback_data.action
    token = callback_data.token
    if action == "dl":
        await _download_callback(query, session, token)
        return
    if action == "zip":
        await _zip_callback(query, session, token)
        return

    card_token, tail = split_token(token)
    stored = _load(session, card_token, RequestType.CATALOGUE_BROWSE)
    if stored is None:
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    payload = stored.payload
    if not isinstance(payload, CataloguePayload):
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    try:
        following = any(
            follow.provider == payload.provider and follow.artist_id == payload.artist_id
            for follow in session.follows.list_for_user(query.from_user.id)
        )
        if action == "follow":
            added = session.follows.toggle(
                query.from_user.id,
                payload.provider,
                payload.artist_id,
                payload.name,
                cover_url=payload.cover_url,
                known_album_ids=[
                    SearchItem.from_dict(raw).item_id for raw in payload.albums
                ],
            )
            await query.answer(
                text.FOLLOW_ADDED.format(name=payload.name)
                if added
                else text.FOLLOW_REMOVED.format(name=payload.name)
            )
            following = added
            card = render_artist_card(stored, following=following)
        elif action == "open":
            index = int(tail or "")
            card = await album_card(session, stored, index)
        elif action == "albums":
            card = render_artist_card(stored, following=following, albums=True)
        elif action == "top":
            card = render_artist_card(stored, following=following, top=True)
        elif action == "menu":
            card = render_artist_card(stored, following=following)
        else:
            await query.answer()
            return
        message = getattr(query, "message", None)
        if message is not None:
            await present_card(
                card, bot=query.bot, surface=CardSurface.CHAT, message=message
            )
        else:
            await present_card(
                card,
                bot=query.bot,
                surface=CardSurface.INLINE,
                inline_message_id=query.inline_message_id,
            )
        await query.answer()
    except (RuntimeError, ValueError, TelegramAPIError) as exc:
        logger.info("Catalogue action failed | action=%s error=%s", action, exc)
        await query.answer(str(exc), show_alert=True)


async def _download_callback(query: CallbackQuery, session: Session, token: str) -> None:
    card_token, track_index = split_token(token)
    card = _load(session, card_token, RequestType.CATALOGUE_BROWSE)
    if card is None:
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    payload = card.payload
    if not isinstance(payload, CataloguePayload):
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    # A media message cannot take a text status, so it reports through the caption.
    caption_status = bool(payload.cover_url)
    try:
        stored = (
            track_request(session, card, int(track_index))
            if track_index is not None
            else album_download_request(session, card)
        )
    except (ValueError, IndexError):
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    message = getattr(query, "message", None)
    if message is not None:
        if stored is None:
            await query.answer(text.REQUEST_EXPIRED, show_alert=True)
            return
        status_message = await message.reply(text.PROCESSING)
        await query.answer()
        await execute_request(
            session,
            stored,
            MUSIC_OPTIONS[0],
            bot=cast(Bot, query.bot),
            status=chat_status(status_message),
            source_message=message,
            user_id=query.from_user.id,
        )
        return
    if stored is None or not query.inline_message_id:
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    await query.answer()
    await run_inline_download(
        session,
        bot=cast(Bot, query.bot),
        user_id=query.from_user.id,
        inline_message_id=query.inline_message_id,
        stored=stored,
        caption_status=caption_status,
    )


async def _zip_callback(query: CallbackQuery, session: Session, token: str) -> None:
    album = _load(session, token, RequestType.CATALOGUE_BROWSE)
    message = getattr(query, "message", None)
    if album is None or (message is None and not query.inline_message_id):
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    payload = album.payload
    if not isinstance(payload, CataloguePayload):
        await query.answer(text.REQUEST_EXPIRED, show_alert=True)
        return
    stored = album_download_request(session, album)
    await query.answer()
    if message is not None:
        await run_chat_album_zip(
            session,
            bot=cast(Bot, query.bot),
            user_id=query.from_user.id,
            source_message=message,
            stored=stored,
        )
        return
    await run_album_zip(
        session,
        bot=cast(Bot, query.bot),
        user_id=query.from_user.id,
        inline_message_id=cast(str, query.inline_message_id),
        stored=stored,
        caption_status=bool(payload.cover_url),
    )




async def _show_failure(query_bot, inline_message_id: str | None, stored: StoredRequest, error: str) -> None:
    if not inline_message_id:
        return
    payload = stored.payload
    message = text.esc(error)
    if isinstance(payload, CataloguePayload) and payload.cover_url:
        await query_bot.edit_message_caption(
            inline_message_id=inline_message_id, caption=message
        )
    else:
        await query_bot.edit_message_text(inline_message_id=inline_message_id, text=message)


def _load(
    session: Session, token: str, request_type: RequestType
) -> StoredRequest | None:
    stored = session.request_store.load(token)
    if stored is None or stored.request_type != request_type:
        return None
    return stored
