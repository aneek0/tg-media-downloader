from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlparse

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from config import Settings
from services.cooldown import CooldownManager
from services.executor import execute_request
from services.gallery import _gallery_friendly_error
from services.media_cache import MediaCache, cached_media_from, send_cached_media
from services.mornye import MornyeService
from services.parsing import (
    extract_link_text,
    is_twitter_status_url,
    parse_user_input,
)
from services.pipeline import prepare_request
from services.request_store import RequestStore
from services.status import chat_status
from services.thumbnail_store import ThumbnailStore
from services.sources import SourceKind, classify
from utils import text
from utils.keyboards import format_keyboard
from utils.logging_config import safe_url_label

router = Router(name="intake")
logger = logging.getLogger(__name__)


@router.message(F.chat.type == "private", F.text)
async def intake_message(  # pylint: disable=too-complex,too-many-positional-arguments
    message: Message,
    settings: Settings,
    cooldown: CooldownManager,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
    media_cache: MediaCache,
    mornye: MornyeService | None = None,
) -> None:
    raw_text = message.text or ""
    if not extract_link_text(raw_text, message.entities):
        return

    if not message.from_user:
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

    cached = media_cache.get(parsed.source_url)
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
            await media_cache.remove(parsed.source_url)
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
        prepared = await prepare_request(
            request_store, parsed, settings, mornye=mornye
        )
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
            stored=stored,
            option=stored.options[0],
            bot=message.bot,
            status=status,
            source_message=message,
            user_id=message.from_user.id,
            settings=settings,
            request_store=request_store,
            thumbnail_store=thumbnail_store,
            media_cache=media_cache,
            mornye=mornye,
        )
        return
    await status.edit(
        text.FORMAT_SELECTION,
        reply_markup=format_keyboard(stored.token, stored.options),
    )
