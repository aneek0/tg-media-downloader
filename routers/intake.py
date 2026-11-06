from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from services.executor import execute_request
from services.gallery import _gallery_friendly_error
from services.media_cache import cached_media_from, send_cached_media
from services.mornye import audio_cache_key
from services.parsing import (
    extract_link_text,
    is_twitter_status_url,
    parse_user_input,
)
from services.pipeline import prepare_request
from services.session import Session
from services.status import chat_status
from services.sources import SourceKind, classify
from utils import text
from utils.keyboards import format_keyboard
from utils.logging_config import safe_url_label

router = Router(name="intake")

def _search_keyboard(query: str) -> InlineKeyboardMarkup:
    choices = (
        (text.SEARCH_TRACKS, ".trk"),
        (text.SEARCH_ALBUMS, ".a"),
        (text.SEARCH_ARTISTS, ".art"),
        (text.SEARCH_PLAYLISTS, ".pla"),
    )
    buttons = [
        InlineKeyboardButton(
            text=label,
            switch_inline_query_current_chat=f"{prefix} {query}",
        )
        for label, prefix in choices
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            buttons[:2],
            buttons[2:],
            [
                InlineKeyboardButton(
                    text=text.SEARCH_GLOBAL,
                    switch_inline_query_current_chat=query,
                )
            ],
        ]
    )
logger = logging.getLogger(__name__)


@router.message(F.chat.type == "private", F.text)
async def intake_message(  # pylint: disable=too-complex
    message: Message, session: Session
) -> None:
    settings = session.settings
    cooldown = session.cooldown
    media_cache = session.media_cache
    mornye = session.mornye
    raw_text = message.text or ""
    if not message.from_user:
        return
    if raw_text.strip().rstrip(".…").casefold() == text.PROCESSING.rstrip(".").casefold():
        return
    if not extract_link_text(raw_text, message.entities):
        preferences = session.preferences(message.from_user.id)
        if preferences.text_search and not raw_text.startswith("/"):
            await message.answer(
                text.SEARCH_PROMPT,
                reply_markup=_search_keyboard(raw_text.strip()),
            )
        return

    parsed = parse_user_input(raw_text, message.entities)
    source = classify(parsed)
    logger.info(
        "Incoming link | user=%s chat=%s source=%s",
        message.from_user.id,
        message.chat.id,
        safe_url_label(parsed.source_url),
    )

    blocked_seconds = cooldown.check(message.from_user.id, settings.auth_users)
    if blocked_seconds:
        minutes = max(1, round(blocked_seconds / 60))
        logger.info(
            "Cooldown blocked | user=%s remaining=%ss",
            message.from_user.id,
            blocked_seconds,
        )
        await message.answer(text.RATE_LIMIT.format(minutes=minutes))
        return

    preferences = session.preferences(message.from_user.id)
    cache_key = audio_cache_key(
        parsed.source_url, preferences.audio_quality, preferences.audio_format
    )
    cached = media_cache.get(cache_key)
    if cached:
        try:
            await send_cached_media(message.bot, message.chat.id, cached)
            logger.info(
                "Cache hit | user=%s source=%s files=%s",
                message.from_user.id,
                safe_url_label(parsed.source_url),
                len(cached),
            )
            return
        except TelegramAPIError as exc:
            logger.info(
                "Cached send failed, purging entry | user=%s source=%s error=%s",
                message.from_user.id,
                safe_url_label(parsed.source_url),
                exc,
            )
            await media_cache.remove(cache_key)
            # fall through to the normal download flow
    status_message = await message.reply(text.PROCESSING)
    status = chat_status(status_message)

    # Fast-path: direct image links are outsourced to Telegram's CDN fetch
    # (symmetry with the inline image branch). On any API error fall
    # through to the normal probe flow.
    if source.kind is SourceKind.IMAGE and not source.is_twitter:
        file_name = (
            parsed.custom_file_name
            or Path(urlparse(parsed.source_url).path).name
            or "photo.jpg"
        )
        try:
            sent = await message.bot.send_photo(
                chat_id=message.chat.id,
                photo=parsed.source_url,
                caption=(parsed.custom_file_name or file_name)[:1024],
            )
        except TelegramAPIError:
            sent = None
        if sent is not None:
            entry = cached_media_from(
                sent,
                send_type="photo",
                file_name=file_name,
                caption=parsed.custom_file_name,
            )
            if entry:
                await media_cache.record(parsed.source_url, [entry])
            await status.done(download_seconds=0, upload_seconds=1)
            return

    if mornye and mornye.enabled and source.kind is SourceKind.MUSIC:
        await status.edit(text.MUSIC_RESOLVING)

    try:
        prepared = await prepare_request(session, parsed)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Request preparation failed | user=%s source=%s error=%s",
            message.from_user.id,
            safe_url_label(parsed.source_url),
            exc,
        )
        await status.fail(
            f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(str(exc))}</code>"
        )
        return

    stored = prepared.stored
    if (
        is_twitter_status_url(parsed.source_url)
        and prepared.gallery.error
        and "Unsupported URL" not in prepared.gallery.error
    ):
        await status.fail(
            f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(_gallery_friendly_error(prepared.gallery.error, bool(settings.twitter_cookies)))}</code>"
        )
        return

    logger.info(
        "Prepared request | user=%s token=%s type=%s source=%s options=%s",
        message.from_user.id,
        stored.token,
        stored.request_type,
        safe_url_label(parsed.source_url),
        len(stored.options),
    )
    if prepared.immediate or settings.auto_best_quality:
        await execute_request(
            session,
            stored,
            stored.options[0],
            bot=message.bot,
            status=status,
            source_message=message,
            user_id=message.from_user.id,
        )
        return
    await status.edit(
        text.FORMAT_SELECTION,
        reply_markup=format_keyboard(stored.token, stored.options),
    )
