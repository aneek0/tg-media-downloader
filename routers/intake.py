from __future__ import annotations

import logging
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import Settings
from services import pixiv, pixiv_flow
from services.catalogue_flow import CardSurface, artist_card, present_card
from services.catalogue_search import artist_source
from services.delivery import cached_media_from, send_cached
from services.executor import execute_request
from services.gallery import _gallery_friendly_error
from services.mornye import audio_cache_key
from services.parsing import (
    extract_link_text,
    is_twitter_status_url,
    parse_user_input,
)
from services.pipeline import prepare_request
from services.session import Session
from services.sources import SourceKind, classify
from services.status import StatusSurface, chat_status
from utils import text
from utils.keyboards import format_keyboard
from utils.logging_config import safe_url_label
from utils.models import ParsedInput


async def _fail_gallery_error(
    status: StatusSurface,
    parsed: ParsedInput,
    error: str | None,
    settings: Settings,
) -> bool:
    """Show the source-specific gallery error, when there is one to show."""
    if not error or "Unsupported URL" in error:
        return False
    if is_twitter_status_url(parsed.source_url):
        message = _gallery_friendly_error(error, bool(settings.twitter_cookies))
    elif pixiv.is_pixiv_url(parsed.source_url):
        message = pixiv.friendly_error(RuntimeError(error))
    else:
        return False
    await status.fail(f"{text.DOWNLOAD_FAILED}\n<code>{text.esc(message)}</code>")
    return True


router = Router(name="intake")
def _is_art_profile_url(raw: str) -> bool:
    """An artist profile URL inserted from our own inline search: those must
    keep flowing into the DM pipeline instead of being swallowed by the
    own-inline feedback gate."""
    if not raw:
        return False
    try:
        parsed = parse_user_input(raw)
    except ValueError:
        return False
    url = parsed.source_url
    return (
        pixiv.pixiv_kind(url) == "profile"
        or _pawchive_user_parts(url) is not None
        or artist_source(url) is not None
    )


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
            [
                InlineKeyboardButton(
                    text=text.SEARCH_PIXIV,
                    switch_inline_query_current_chat=f".pxv {query}",
                ),
                InlineKeyboardButton(
                    text=text.SEARCH_PAWCHIVE,
                    switch_inline_query_current_chat=f".paw {query}",
                ),
            ],
        ]
    )
logger = logging.getLogger(__name__)
async def _handle_pawchive(
    session: Session,
    message: Message,
    parsed: ParsedInput,
    status: StatusSurface,
) -> bool:
    """True when the message was a Pawchive creator page and this handler
    owns it. A creator page is browsed post by post, never enumerated."""
    parts = _pawchive_user_parts(parsed.source_url)
    if parts is None:
        return False
    service, user_id = parts
    from services.pawchive_flow import open_profile

    await open_profile(
        session,
        bot=cast(Bot, message.bot),
        message=message,
        service=service,
        user_id=user_id,
        status=status,
    )
    return True


def _pawchive_user_parts(url: str) -> tuple[str, str] | None:
    """(service, user_id) for a pawchive creator page URL, else None."""
    host = (urlparse(url).hostname or "").lower()
    if host != "pawchive.pw" and not host.endswith(".pawchive.pw"):
        return None
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 3 or parts[1] != "user":
        return None
    # A post URL (/SERVICE/user/ID/post/...) is a media page, not a profile.
    if len(parts) > 3:
        return None
    return parts[0], parts[2]

async def _answer_artist_card(
    message: Message, session: Session, provider: str, artist_id: str
) -> None:
    """Answer an artist Source URL with a normal bot-authored Artist Card."""
    if not message.from_user:
        return
    try:
        following = any(
            follow.provider == provider and follow.artist_id == artist_id
            for follow in session.follows.list_for_user(message.from_user.id)
        )
        card = await artist_card(
            session,
            provider=provider,
            artist_id=artist_id,
            following=following,
        )
        await present_card(card, surface=CardSurface.ANSWER, message=message)
    except (RuntimeError, ValueError, TelegramAPIError) as exc:
        logger.info("Artist Card failed | user=%s error=%s", message.from_user.id, exc)
        await message.answer(text.esc(str(exc)))

async def _handle_pixiv(
    session: Session,
    message: Message,
    parsed: ParsedInput,
    status: StatusSurface,
) -> bool:
    """True when the message was a Pixiv link and this handler owns it.

    A profile is browsed page by page, never enumerated through gallery-dl
    (which cannot seek into a profile); link kinds outside the supported set
    are rejected instead of falling through to the generic ladder.
    """
    if not pixiv.is_pixiv_url(parsed.source_url):
        return False
    kind = pixiv.pixiv_kind(parsed.source_url)
    if kind == "unsupported":
        await status.fail(text.PIXIV_UNSUPPORTED)
        return True
    # A profile only needs Pixiv's public web endpoints; an artwork download
    # needs gallery-dl credentials, which may come from its own OAuth cache.
    if kind == "work" and not pixiv.credentials_available(session.settings):
        await status.fail(text.PIXIV_NOT_CONFIGURED)
        return True
    if kind == "profile":
        await pixiv_flow.open_profile(
            session,
            bot=cast(Bot, message.bot),
            message=message,
            source_url=parsed.source_url,
            status=status,
        )
        return True
    return False



@router.message(F.chat.type == "private", F.text)
async def intake_message(  # noqa: C901, PLR0911
    message: Message, session: Session
) -> None:
    settings = session.settings
    cooldown = session.cooldown
    media_cache = session.media_cache
    mornye = session.mornye
    raw_text = message.text or ""
    via_bot = message.via_bot
    own_inline = via_bot and via_bot.id == cast(Bot, message.bot).id
    if own_inline and not _is_art_profile_url(raw_text):
        # Telegram returns the bot's own inline article as a DM from the bot
        # with via_bot = the bot. Music artist links must not re-trigger, but
        # a Pixiv/Pawchive profile link is meant to open that profile.
        return
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

    artist = artist_source(parsed.source_url)
    if artist is not None:
        await _answer_artist_card(message, session, *artist)
        return

    preferences = session.preferences(message.from_user.id)
    cache_key = audio_cache_key(
        parsed.source_url, preferences.audio_quality, preferences.audio_format
    )
    cached = media_cache.get(cache_key)
    if cached:
        try:
            await send_cached(cast(Bot, message.bot), message.chat.id, cached)
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
    if (
        source.kind is SourceKind.IMAGE
        and not source.is_twitter
        and not pixiv.is_pixiv_image_url(parsed.source_url)
    ):
        file_name = (
            parsed.custom_file_name
            or Path(urlparse(parsed.source_url).path).name
            or "photo.jpg"
        )
        try:
            # aiogram binds a bot to every dispatched update.
            sent = await cast(Bot, message.bot).send_photo(
                chat_id=message.chat.id,
                photo=parsed.source_url,
                caption=text.esc(
                    (parsed.custom_file_name or file_name)[: text.CAPTION_LIMIT]
                ),
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

    if await _handle_pawchive(session, message, parsed, status):
        return

    if await _handle_pixiv(session, message, parsed, status):
        return

    if mornye and mornye.enabled and source.kind is SourceKind.MUSIC:
        await status.edit(text.MUSIC_RESOLVING)

    try:
        prepared = await prepare_request(session, parsed)
    except Exception as exc:  # noqa: BLE001
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
    if await _fail_gallery_error(status, parsed, prepared.gallery.error, settings):
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
            bot=cast(Bot, message.bot),
            status=status,
            source_message=message,
            user_id=message.from_user.id,
        )
        return
    await status.edit(
        text.FORMAT_SELECTION,
        reply_markup=format_keyboard(stored.token, stored.options),
    )
