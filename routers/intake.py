from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import Message

from config import Settings
from services.cooldown import CooldownManager
from services.executor import execute_request
from services.parsing import (
    extract_link_text,
    is_probable_youtube_url,
    is_twitter_status_url,
    parse_user_input,
)
from services.request_store import RequestStore
from services.thumbnail_store import ThumbnailStore
from services.ytdlp import (
    build_direct_options,
    build_quick_youtube_options,
    build_ytdlp_options,
    probe_url,
)
from utils import text
from utils.keyboards import format_keyboard
from utils.logging_config import safe_url_label
from utils.models import DownloadOption, ParsedInput, StoredRequest

router = Router(name="intake")
logger = logging.getLogger(__name__)


@router.message(F.chat.type == "private", F.text)
async def intake_message(
    message: Message,
    settings: Settings,
    cooldown: CooldownManager,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
) -> None:
    raw_text = message.text or ""
    if not extract_link_text(raw_text, message.entities):
        return

    if not message.from_user:
        return

    logger.info(
        "Incoming link | user=%s chat=%s source=%s",
        message.from_user.id,
        message.chat.id,
        safe_url_label(parse_user_input(raw_text, message.entities).source_url),
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

    parsed = parse_user_input(raw_text, message.entities)
    status_message = await message.reply(text.PROCESSING)

    if is_twitter_status_url(parsed.source_url):
        await _run_gallery_download(
            message=message,
            parsed=parsed,
            status_message=status_message,
            settings=settings,
            request_store=request_store,
            thumbnail_store=thumbnail_store,
        )
        return

    if settings.auto_best_quality:
        await _run_auto_best(
            message=message,
            parsed=parsed,
            status_message=status_message,
            settings=settings,
            request_store=request_store,
            thumbnail_store=thumbnail_store,
        )
        return

    if is_probable_youtube_url(parsed.source_url):
        token = request_store.create_token()
        stored = StoredRequest(
            token=token,
            request_type="youtube_quick",
            parsed_input=parsed,
            options=build_quick_youtube_options(),
        )
        request_store.save(stored)
        logger.info(
            "Prepared quick YouTube request | user=%s token=%s source=%s options=%s",
            message.from_user.id,
            token,
            safe_url_label(parsed.source_url),
            len(stored.options),
        )
        await status_message.edit_text(
            text.QUICK_CHOICE,
            reply_markup=format_keyboard(token, stored.options),
        )
        return

    try:
        info = await probe_url(parsed, settings)
    except RuntimeError as exc:  # pragma: no cover - network/tool error path
        logger.warning(
            "yt-dlp probe failed | user=%s source=%s error=%s",
            message.from_user.id,
            safe_url_label(parsed.source_url),
            exc,
        )
        info = None

    token = request_store.create_token()
    if info:
        options = build_ytdlp_options(info)
        request_type = "ytdlp_selection"
        if not options:
            options = build_direct_options(parsed, info=info)
            request_type = "direct_download"
    else:
        options = build_direct_options(parsed, info=None)
        request_type = "direct_download"

    stored = StoredRequest(
        token=token,
        request_type=request_type,
        parsed_input=parsed,
        options=options,
        info=info or {},
    )
    request_store.save(stored)
    logger.info(
        "Prepared request | user=%s token=%s type=%s source=%s options=%s title=%s",
        message.from_user.id,
        token,
        request_type,
        safe_url_label(parsed.source_url),
        len(options),
        (info or {}).get("title", "-"),
    )
    await status_message.edit_text(
        text.FORMAT_SELECTION,
        reply_markup=format_keyboard(token, options),
    )


async def _run_auto_best(
    *,
    message: Message,
    parsed: ParsedInput,
    status_message: Message,
    settings: Settings,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
) -> None:
    try:
        info = await probe_url(parsed, settings)
    except RuntimeError as exc:  # pragma: no cover - network/tool error path
        logger.warning(
            "yt-dlp probe failed | user=%s source=%s error=%s",
            message.from_user.id,
            safe_url_label(parsed.source_url),
            exc,
        )
        info = None

    token = request_store.create_token()
    if info:
        options = [
            DownloadOption(
                option_id="auto_best",
                label="Best quality",
                send_type="video",
                mode="ytdlp_auto",
            )
        ]
        request_type = "ytdlp_auto"
    else:
        options = build_direct_options(parsed, info=None)
        request_type = "direct_download"

    stored = StoredRequest(
        token=token,
        request_type=request_type,
        parsed_input=parsed,
        options=options,
        info=info or {},
    )
    request_store.save(stored)
    logger.info(
        "Prepared auto request | user=%s token=%s type=%s source=%s title=%s",
        message.from_user.id,
        token,
        request_type,
        safe_url_label(parsed.source_url),
        (info or {}).get("title", "-"),
    )
    await execute_request(
        stored=stored,
        option=stored.options[0],
        bot=message.bot,
        status_message=status_message,
        source_message=message,
        user_id=message.from_user.id,
        settings=settings,
        request_store=request_store,
        thumbnail_store=thumbnail_store,
    )


async def _run_gallery_download(
    *,
    message: Message,
    parsed: ParsedInput,
    status_message: Message,
    settings: Settings,
    request_store: RequestStore,
    thumbnail_store: ThumbnailStore,
) -> None:
    token = request_store.create_token()
    stored = StoredRequest(
        token=token,
        request_type="gallery_media",
        parsed_input=parsed,
        options=[
            DownloadOption(
                option_id="gallery_all",
                label="Media",
                send_type="photo",
                mode="gallery",
            )
        ],
    )
    request_store.save(stored)
    logger.info(
        "Prepared gallery request | user=%s token=%s source=%s",
        message.from_user.id,
        token,
        safe_url_label(parsed.source_url),
    )
    await execute_request(
        stored=stored,
        option=stored.options[0],
        bot=message.bot,
        status_message=status_message,
        source_message=message,
        user_id=message.from_user.id,
        settings=settings,
        request_store=request_store,
        thumbnail_store=thumbnail_store,
    )
